"""
Takes a list of source file paths, parses each with Tree-sitter (Python/JS/TS/TSX),
splits markdown by heading, and treats config/shell/markup files as whole-file chunks.
All chunks are then split as needed to fit within the embedding model token limit.
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

EMBEDDING_MODEL = "text-embedding-3-large"
EMBEDDING_CONTEXT_WINDOW_TOKENS = int(os.environ.get("CODE_EMBEDDING_CONTEXT_WINDOW_TOKENS", "8191"))
EMBEDDING_SAFETY_MARGIN_TOKENS = int(os.environ.get("CODE_EMBEDDING_SAFETY_MARGIN_TOKENS", "256"))
EMBEDDING_HARD_MAX_TOKENS = max(1, EMBEDDING_CONTEXT_WINDOW_TOKENS - EMBEDDING_SAFETY_MARGIN_TOKENS)

# Retrieval quality is better when code chunks stay focused. Keep the hard
# model limit as a final guard, but split ordinary code well below it.
MAX_TOKENS = min(
    EMBEDDING_HARD_MAX_TOKENS,
    int(os.environ.get("CODE_EMBEDDING_TARGET_TOKENS", "1800")),
)
OVERLAP_LINES = int(os.environ.get("CODE_EMBEDDING_OVERLAP_LINES", "8"))
OVERLAP_TOKENS = int(os.environ.get("CODE_EMBEDDING_OVERLAP_TOKENS", "96"))
EMBEDDING_BATCH_SIZE = int(os.environ.get("CODE_EMBEDDING_BATCH_SIZE", "64"))

encoder = tiktoken.encoding_for_model(EMBEDDING_MODEL)
_openai_client: OpenAI | None = None


def _client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI()
    return _openai_client


def embed_query(text: str) -> list[float]:
    """Embed a single query string with the same model used for chunks."""
    return embed_texts([text], allow_truncate=True)[0]


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
    embedding_path: str | None = None
    chunk_sha256: str | None = None
    embedding_sha256: str | None = None
    embedding_model: str = EMBEDDING_MODEL
    embedding: list[float] | None = None

    @property
    def embedding_text(self) -> str:
        """What actually gets sent to the embedding model."""
        prefix_parts = [f"# File: {self.embedding_path or self.file_path}"]
        if self.parent_class:
            prefix_parts.append(f"# Class: {self.parent_class}")
        if self.name:
            prefix_parts.append(f"# {self.chunk_type}: {self.name}")
        prefix_parts.append(self.content)
        return "\n".join(prefix_parts)

    @property
    def token_count(self) -> int:
        return len(encoder.encode(self.embedding_text))


def _embedding_prefix_token_count(chunk: CodeChunk) -> int:
    probe = CodeChunk(
        content="",
        chunk_type=chunk.chunk_type,
        file_path=chunk.file_path,
        name=chunk.name,
        parent_class=chunk.parent_class,
    )
    probe.embedding_path = chunk.embedding_path
    return probe.token_count


def _clone_piece(
    chunk: CodeChunk,
    *,
    content: str,
    part_num: int,
    start_line: int,
    end_line: int,
    split_strategy: str,
) -> CodeChunk:
    piece_name = f"{chunk.name} (part {part_num})" if chunk.name else f"part {part_num}"
    piece = CodeChunk(
        content=content,
        chunk_type=chunk.chunk_type,
        file_path=chunk.file_path,
        name=piece_name,
        parent_class=chunk.parent_class,
        start_line=start_line,
        end_line=end_line,
        metadata={
            **chunk.metadata,
            "part": part_num,
            "split_strategy": split_strategy,
            "source_start_line": chunk.start_line,
            "source_end_line": chunk.end_line,
        },
        embedding_path=chunk.embedding_path,
        embedding_model=chunk.embedding_model,
    )
    return piece


def _split_text_by_tokens(
    chunk: CodeChunk,
    *,
    text: str,
    start_line: int,
    end_line: int,
    first_part_num: int,
) -> list[CodeChunk]:
    prefix_tokens = _embedding_prefix_token_count(chunk)
    available_tokens = max(1, MAX_TOKENS - prefix_tokens)
    overlap = min(OVERLAP_TOKENS, max(0, available_tokens // 8))
    tokens = encoder.encode(text)
    pieces: list[CodeChunk] = []
    token_start = 0

    while token_start < len(tokens):
        token_end = min(token_start + available_tokens, len(tokens))
        while True:
            content = encoder.decode(tokens[token_start:token_end])
            piece = _clone_piece(
                chunk,
                content=content,
                part_num=first_part_num + len(pieces),
                start_line=start_line,
                end_line=end_line,
                split_strategy="token",
            )
            if piece.token_count <= MAX_TOKENS or token_end <= token_start + 1:
                break
            token_end -= max(1, piece.token_count - MAX_TOKENS + 8)
        pieces.append(piece)
        if token_end >= len(tokens):
            break
        token_start = max(token_end - overlap, token_start + 1)

    return pieces


def split_oversized(chunk: CodeChunk) -> list[CodeChunk]:
    """
    If a chunk exceeds MAX_TOKENS, split it at line boundaries
    with overlap between consecutive pieces. Very long single lines are
    split by token window so no embedding request can exceed the model limit.
    """
    if chunk.token_count <= MAX_TOKENS:
        return [chunk]

    lines = chunk.content.splitlines(keepends=True)
    if not lines:
        return [chunk]

    pieces: list[CodeChunk] = []
    start_idx = 0

    while start_idx < len(lines):
        end_idx = start_idx
        current_text = ""

        while end_idx < len(lines):
            line = lines[end_idx]
            line_probe = CodeChunk(
                content=line,
                chunk_type=chunk.chunk_type,
                file_path=chunk.file_path,
                name=chunk.name,
                parent_class=chunk.parent_class,
            )
            line_probe.embedding_path = chunk.embedding_path
            line_probe.embedding_model = chunk.embedding_model
            if line_probe.token_count > MAX_TOKENS:
                if current_text:
                    break
                pieces.extend(_split_text_by_tokens(
                    chunk,
                    text=line,
                    start_line=chunk.start_line + start_idx,
                    end_line=chunk.start_line + start_idx,
                    first_part_num=len(pieces) + 1,
                ))
                end_idx += 1
                current_text = ""
                break

            candidate = current_text + line
            temp = CodeChunk(
                content=candidate,
                chunk_type=chunk.chunk_type,
                file_path=chunk.file_path,
                name=chunk.name,
                parent_class=chunk.parent_class,
            )
            temp.embedding_path = chunk.embedding_path
            temp.embedding_model = chunk.embedding_model
            if temp.token_count > MAX_TOKENS:
                break
            current_text = candidate
            end_idx += 1

        if current_text:
            pieces.append(_clone_piece(
                chunk,
                content=current_text,
                part_num=len(pieces) + 1,
                start_line=chunk.start_line + start_idx,
                end_line=chunk.start_line + end_idx - 1,
                split_strategy="line",
            ))

        if end_idx >= len(lines):
            break
        start_idx = max(end_idx - OVERLAP_LINES, start_idx + 1)

    return pieces


def _truncate_embedding_text(text: str, max_tokens: int = EMBEDDING_HARD_MAX_TOKENS) -> str:
    tokens = encoder.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return encoder.decode(tokens[:max_tokens])


def embed_texts(
    texts: list[str],
    *,
    model: str = EMBEDDING_MODEL,
    allow_truncate: bool = False,
) -> list[list[float]]:
    """Embed raw text with deterministic preflight validation.

    Code chunks should be split before this point, so oversize inputs fail with
    local diagnostics instead of a remote provider error. Query/summary callers
    may opt into truncation where dropping tail context is acceptable.
    """
    if allow_truncate:
        prepared = [_truncate_embedding_text(text) for text in texts]
    else:
        prepared = texts
        oversize = [
            (idx, len(encoder.encode(text)))
            for idx, text in enumerate(prepared)
            if len(encoder.encode(text)) > EMBEDDING_HARD_MAX_TOKENS
        ]
        if oversize:
            details = ", ".join(
                f"input[{idx}]={count} tokens" for idx, count in oversize[:5]
            )
            raise ValueError(
                "Embedding input exceeds model token budget after chunking: "
                f"{details}. Hard max is {EMBEDDING_HARD_MAX_TOKENS} tokens."
            )

    embeddings: list[list[float]] = []
    for i in range(0, len(prepared), EMBEDDING_BATCH_SIZE):
        batch = prepared[i:i + EMBEDDING_BATCH_SIZE]
        resp = _client().embeddings.create(input=batch, model=model)
        embeddings.extend(datum.embedding for datum in resp.data)
    return embeddings


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
    ".json": "config", ".toml": "config", ".yaml": "config", ".yml": "config",
    ".ini": "config", ".cfg": "config", ".conf": "config",
    ".env": "config", ".example": "config", ".sample": "config", ".template": "config",
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


def chunk_file_list(file_paths: list[str], *, embed: bool = True) -> list[CodeChunk]:
    """
    Main entry point. Takes a pre-filtered list of source file paths,
    dispatches to the right extractor by extension, then splits any
    oversized chunks to fit the embedding model token limit.

    Supported extensions:
      .py .js .jsx .ts .tsx .md .json .toml .yaml .yml .ini .cfg .conf .css .html .sh
    """
    all_chunks = []
    for path in file_paths:
        with open(path, "rb") as f:
            source = f.read()
        raw_chunks = _dispatch_extract(source, file_path=path)
        for chunk in raw_chunks:
            all_chunks.extend(split_oversized(chunk))

    if not embed:
        return all_chunks

    embed_chunks(all_chunks)
    return all_chunks


def embed_chunks(chunks: list[CodeChunk]) -> int:
    """Embed chunks that do not already have an embedding. Returns misses embedded."""
    pending = [c for c in chunks if c.embedding is None]
    if not pending:
        return 0

    embeddings = embed_texts([c.embedding_text for c in pending])
    for chunk, embedding in zip(pending, embeddings):
        chunk.embedding = embedding
    return len(pending)
