"""
Takes a list of source file paths, parses each with Tree-sitter (Python/JS/TS/TSX),
splits markdown by heading, and treats config/shell/markup files as whole-file chunks.
All chunks are then split as needed to fit within text-embedding-3-small's 8191 token limit.
"""

import os
import re
import tree_sitter_python as tspython
import tree_sitter_javascript as tsjavascript
import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Parser
from dataclasses import dataclass, field
from openai import OpenAI
import tiktoken

py_parser = Parser(Language(tspython.language()))
js_parser = Parser(Language(tsjavascript.language()))
ts_parser = Parser(Language(tstypescript.language_typescript()))
tsx_parser = Parser(Language(tstypescript.language_tsx()))

AST_PARSERS = {
    ".py": py_parser,
    ".js": js_parser, ".jsx": js_parser,
    ".ts": ts_parser, ".tsx": tsx_parser,
}

# text-embedding-3-small limit is 8191, leave buffer for metadata prefix
MAX_TOKENS = 7500
OVERLAP_LINES = 5  # lines of overlap when splitting oversized chunks

encoder = tiktoken.encoding_for_model("text-embedding-3-large") # switched to large model
client = OpenAI()


def embed_query(text: str) -> list[float]:
    """Embed a single query string with the same model used for chunks."""
    return client.embeddings.create(
        input=[text], model="text-embedding-3-large"
    ).data[0].embedding


def dump_ast(node, src: bytes, depth: int = 0, max_depth: int | None = 3) -> list[str]:
    """Flatten a tree-sitter AST into indented `type [start:end]  'snippet'` lines."""
    if max_depth is not None and depth > max_depth:
        return []
    snippet = src[node.start_byte:node.end_byte].decode(errors="replace").split("\n")[0][:60]
    lines = [f"{'  ' * depth}{node.type} [{node.start_point[0]}:{node.end_point[0]}]  {snippet!r}"]
    for child in node.children:
        lines.extend(dump_ast(child, src, depth + 1, max_depth))
    return lines

@dataclass
class CodeChunk:
    content: str
    chunk_type: str  # "function", "class", "method", "imports", "module_docstring"
    file_path: str
    name: str | None = None
    parent_class: str | None = None
    start_line: int = 0
    end_line: int = 0
    metadata: dict = field(default_factory=dict)
    embedding: list[float] | None = None

    @property
    def embedding_text(self) -> str:
        """What actually gets sent to the embedding model."""
        prefix_parts = [f"# File: {self.file_path}"]
        if self.parent_class:
            prefix_parts.append(f"# Class: {self.parent_class}")
        if self.name:
            prefix_parts.append(f"# {self.chunk_type}: {self.name}")
        prefix_parts.append(self.content)
        return "\n".join(prefix_parts)

    @property
    def token_count(self) -> int:
        return len(encoder.encode(self.embedding_text))


def split_oversized(chunk: CodeChunk) -> list[CodeChunk]:
    """
    If a chunk exceeds MAX_TOKENS, split it at line boundaries
    with OVERLAP_LINES of overlap between consecutive pieces.
    """
    if chunk.token_count <= MAX_TOKENS:
        return [chunk]

    lines = chunk.content.splitlines(keepends=True)
    pieces: list[CodeChunk] = []
    start_idx = 0

    while start_idx < len(lines):
        end_idx = start_idx
        current_text = ""

        while end_idx < len(lines):
            candidate = current_text + lines[end_idx]
            temp = CodeChunk(
                content=candidate,
                chunk_type=chunk.chunk_type,
                file_path=chunk.file_path,
                name=chunk.name,
                parent_class=chunk.parent_class,
            )
            if temp.token_count > MAX_TOKENS and end_idx > start_idx:
                break
            current_text = candidate
            end_idx += 1

        part_num = len(pieces) + 1
        pieces.append(CodeChunk(
            content=current_text,
            chunk_type=chunk.chunk_type,
            file_path=chunk.file_path,
            name=f"{chunk.name} (part {part_num})" if chunk.name else f"part {part_num}",
            parent_class=chunk.parent_class,
            start_line=chunk.start_line + start_idx,
            end_line=chunk.start_line + end_idx - 1,
            metadata={**chunk.metadata, "part": part_num},
        ))

        if end_idx >= len(lines):
            break
        start_idx = max(end_idx - OVERLAP_LINES, start_idx + 1)

    return pieces


def get_node_name(node) -> str | None:
    """Extract the name from a function/class definition node."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        # Might be a decorated_definition wrapping the actual def/class
        inner = node.child_by_field_name("definition")
        if inner:
            name_node = inner.child_by_field_name("name")
    return name_node.text.decode() if name_node else None


def get_body(node):
    """Get the body node, handling decorated_definitions."""
    body = node.child_by_field_name("body")
    if body is None:
        inner = node.child_by_field_name("definition")
        if inner:
            body = inner.child_by_field_name("body")
    return body


def extract_chunks_from_file(source: bytes, file_path: str) -> list[CodeChunk]:
    """
    Parse one Python file and return semantic chunks:
    - module-level docstring
    - import block (all imports grouped)
    - top-level functions (including decorated)
    - class definitions — kept whole if <60 lines, otherwise split into:
        - class header + docstring
        - individual methods
    """
    tree = py_parser.parse(source)
    root = tree.root_node
    chunks = []

    # --- Module docstring ---
    if root.children:
        first = root.children[0]
        if first.type == "expression_statement" and first.children:
            expr = first.children[0]
            if expr.type == "string":
                chunks.append(CodeChunk(
                    content=expr.text.decode(),
                    chunk_type="module_docstring",
                    file_path=file_path,
                    name="module_docstring",
                    start_line=expr.start_point[0],
                    end_line=expr.end_point[0],
                ))

    # --- Imports (grouped into one chunk) ---
    import_lines = []
    import_start = import_end = None
    for node in root.children:
        if node.type in ("import_statement", "import_from_statement"):
            if import_start is None:
                import_start = node.start_point[0]
            import_end = node.end_point[0]
            import_lines.append(node.text.decode())

    if import_lines:
        chunks.append(CodeChunk(
            content="\n".join(import_lines),
            chunk_type="imports",
            file_path=file_path,
            name="imports",
            start_line=import_start,
            end_line=import_end,
        ))

    # --- Top-level functions and classes ---
    for node in root.children:
        if node.type == "function_definition":
            chunks.append(CodeChunk(
                content=node.text.decode(),
                chunk_type="function",
                file_path=file_path,
                name=get_node_name(node),
                start_line=node.start_point[0],
                end_line=node.end_point[0],
            ))

        elif node.type == "decorated_definition":
            inner = node.child_by_field_name("definition")
            if inner and inner.type == "function_definition":
                chunks.append(CodeChunk(
                    content=node.text.decode(),
                    chunk_type="function",
                    file_path=file_path,
                    name=get_node_name(node),
                    start_line=node.start_point[0],
                    end_line=node.end_point[0],
                ))
            elif inner and inner.type == "class_definition":
                chunks.extend(_extract_class(node, file_path))

        elif node.type == "class_definition":
            chunks.extend(_extract_class(node, file_path))

    return chunks


def _extract_class(node, file_path: str) -> list[CodeChunk]:
    """
    Small class (<60 lines) → one chunk.
    Large class → header chunk + one chunk per method.
    """
    class_name = get_node_name(node) or "<anonymous>"
    class_text = node.text.decode()
    line_count = node.end_point[0] - node.start_point[0]

    if line_count < 60:
        return [CodeChunk(
            content=class_text,
            chunk_type="class",
            file_path=file_path,
            name=class_name,
            start_line=node.start_point[0],
            end_line=node.end_point[0],
        )]

    # Large class: split into header + individual methods
    chunks = []
    body = get_body(node)
    if not body:
        return [CodeChunk(
            content=class_text,
            chunk_type="class",
            file_path=file_path,
            name=class_name,
            start_line=node.start_point[0],
            end_line=node.end_point[0],
        )]

    # Class header (signature + docstring)
    header_end = body.start_byte - node.start_byte
    header_text = class_text[:header_end].rstrip()

    # Grab class-level docstring if present
    docstring = ""
    if body.children:
        first = body.children[0]
        if first.type == "expression_statement" and first.children:
            expr = first.children[0]
            if expr.type == "string":
                docstring = "\n    " + expr.text.decode()

    chunks.append(CodeChunk(
        content=header_text + docstring,
        chunk_type="class",
        file_path=file_path,
        name=f"{class_name} (header)",
        start_line=node.start_point[0],
        end_line=node.start_point[0] + header_text.count("\n"),
    ))

    # Individual methods
    for child in body.children:
        if child.type in ("function_definition", "decorated_definition"):
            chunks.append(CodeChunk(
                content=child.text.decode(),
                chunk_type="method",
                file_path=file_path,
                name=get_node_name(child),
                parent_class=class_name,
                start_line=child.start_point[0],
                end_line=child.end_point[0],
            ))

    return chunks


JS_CHUNK_TYPES = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
    "lexical_declaration": "declaration",
    "variable_declaration": "declaration",
}
JS_TOP_LEVEL_DECLS = JS_CHUNK_TYPES.keys()
JS_IMPORT_TYPES = {"import_statement", "import"}


def _js_node_name(node) -> str | None:
    name_node = node.child_by_field_name("name")
    if name_node:
        return name_node.text.decode(errors="replace")
    # lexical/variable declaration: first declarator's name
    for child in node.children:
        if child.type in ("variable_declarator",):
            n = child.child_by_field_name("name")
            if n:
                return n.text.decode(errors="replace")
    return None


def extract_js_chunks(source: bytes, file_path: str, parser_: Parser) -> list[CodeChunk]:
    """
    Parse a JS/JSX/TS/TSX file and extract top-level semantic chunks:
    imports (grouped), functions, classes, const/let declarations,
    type aliases, interfaces, and exported variants thereof.
    """
    tree = parser_.parse(source)
    root = tree.root_node
    chunks: list[CodeChunk] = []

    import_nodes = []
    for node in root.children:
        target = node
        # Unwrap `export` statements to inspect the inner declaration
        if node.type == "export_statement":
            decl = node.child_by_field_name("declaration")
            if decl:
                target = decl

        if node.type in JS_IMPORT_TYPES:
            import_nodes.append(node)
            continue

        if target.type in JS_TOP_LEVEL_DECLS:
            name = _js_node_name(target) or "<anonymous>"
            chunks.append(CodeChunk(
                content=node.text.decode(errors="replace"),
                chunk_type=JS_CHUNK_TYPES[target.type],
                file_path=file_path,
                name=name,
                start_line=node.start_point[0],
                end_line=node.end_point[0],
            ))

    if import_nodes:
        chunks.insert(0, CodeChunk(
            content="\n".join(n.text.decode(errors="replace") for n in import_nodes),
            chunk_type="imports",
            file_path=file_path,
            name="imports",
            start_line=import_nodes[0].start_point[0],
            end_line=import_nodes[-1].end_point[0],
        ))

    # If we couldn't extract anything meaningful (e.g. mostly JSX or top-level calls),
    # fall back to the whole file as a single chunk.
    if not chunks:
        text = source.decode(errors="replace")
        chunks.append(CodeChunk(
            content=text,
            chunk_type="file",
            file_path=file_path,
            name=os.path.basename(file_path),
            start_line=0,
            end_line=text.count("\n"),
        ))

    return chunks


def extract_markdown_chunks(source: bytes, file_path: str) -> list[CodeChunk]:
    """Split markdown by top-level (#, ##, ###) headings."""
    text = source.decode(errors="replace")
    lines = text.splitlines(keepends=True)
    heading_re = re.compile(r"^#{1,6}\s+")

    sections: list[tuple[str, int, str]] = []  # (heading, start_line, buffer)
    current_heading = os.path.basename(file_path)
    current_start = 0
    current_buf: list[str] = []

    for i, line in enumerate(lines):
        if heading_re.match(line):
            if current_buf:
                sections.append((current_heading, current_start, "".join(current_buf)))
            current_heading = line.strip().lstrip("#").strip() or f"section@{i}"
            current_start = i
            current_buf = [line]
        else:
            current_buf.append(line)

    if current_buf:
        sections.append((current_heading, current_start, "".join(current_buf)))

    return [
        CodeChunk(
            content=content,
            chunk_type="markdown_section",
            file_path=file_path,
            name=heading,
            start_line=start,
            end_line=start + content.count("\n"),
        )
        for heading, start, content in sections
    ]


def extract_whole_file_chunk(source: bytes, file_path: str, chunk_type: str) -> list[CodeChunk]:
    """Treat the file as a single chunk; split_oversized handles size."""
    text = source.decode(errors="replace")
    return [CodeChunk(
        content=text,
        chunk_type=chunk_type,
        file_path=file_path,
        name=os.path.basename(file_path),
        start_line=0,
        end_line=text.count("\n"),
    )]


_JS_EXT_PARSERS = {".js": js_parser, ".jsx": js_parser, ".ts": ts_parser, ".tsx": tsx_parser}
_WHOLE_FILE_TYPES = {
    ".json": "config", ".yaml": "config", ".yml": "config",
    ".css": "stylesheet",
    ".html": "markup", ".htm": "markup",
    ".sh": "shell",
}


def _dispatch_extract(source: bytes, file_path: str) -> list[CodeChunk]:
    """Pick an extractor by file extension. Unknown extensions become whole-file chunks."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".py":
        return extract_chunks_from_file(source, file_path)
    if ext in _JS_EXT_PARSERS:
        return extract_js_chunks(source, file_path, _JS_EXT_PARSERS[ext])
    if ext in (".md", ".markdown"):
        return extract_markdown_chunks(source, file_path)
    return extract_whole_file_chunk(source, file_path, _WHOLE_FILE_TYPES.get(ext, "file"))


def chunk_file_list(file_paths: list[str]) -> list[CodeChunk]:
    """
    Main entry point. Takes a pre-filtered list of source file paths,
    dispatches to the right extractor by extension, then splits any
    oversized chunks to fit the embedding model token limit.

    Supported extensions:
      .py .js .jsx .ts .tsx .md .json .yaml .yml .css .html .sh
    """
    all_chunks = []
    for path in file_paths:
        with open(path, "rb") as f:
            source = f.read()
        raw_chunks = _dispatch_extract(source, file_path=path)
        for chunk in raw_chunks:
            all_chunks.extend(split_oversized(chunk))

    BATCH_SIZE = 100
    for i in range(0, len(all_chunks), BATCH_SIZE):
        batch = all_chunks[i:i + BATCH_SIZE]
        resp = client.embeddings.create(
            input=[c.embedding_text for c in batch],
            model="text-embedding-3-large",
        )
        for chunk, datum in zip(batch, resp.data):
            chunk.embedding = datum.embedding

    return all_chunks