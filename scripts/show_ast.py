"""
show_ast.py — print the tree-sitter AST for a source file.

Usage:
    python3 scripts/show_ast.py <file> [--max-depth N]

Supported extensions: .py .js .jsx .ts .tsx
"""

import argparse
import os
import sys

# Make the repo root importable when run as `python3 scripts/show_ast.py`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.chunk_and_embed import py_parser, js_parser, ts_parser, tsx_parser

PARSERS = {
    ".py": py_parser,
    ".js": js_parser,
    ".jsx": js_parser,
    ".ts": ts_parser,
    ".tsx": tsx_parser,
}


def dump(node, src: bytes, depth: int = 0, max_depth: int | None = None) -> None:
    if max_depth is not None and depth > max_depth:
        return
    snippet = (
        src[node.start_byte:node.end_byte]
        .decode(errors="replace")
        .split("\n")[0][:60]
    )
    field_name = ""
    print(
        f"{'  ' * depth}{node.type} "
        f"[{node.start_point[0]}:{node.end_point[0]}]  {snippet!r}"
    )
    for child in node.children:
        dump(child, src, depth + 1, max_depth)


def main() -> None:
    ap = argparse.ArgumentParser(description="Print the tree-sitter AST for a file.")
    ap.add_argument("path", help="path to a .py/.js/.jsx/.ts/.tsx file")
    ap.add_argument("--max-depth", type=int, default=None, help="limit recursion depth")
    args = ap.parse_args()

    ext = os.path.splitext(args.path)[1].lower()
    parser_ = PARSERS.get(ext)
    if parser_ is None:
        sys.exit(f"No tree-sitter parser for extension {ext!r}. "
                 f"Supported: {sorted(PARSERS.keys())}")

    with open(args.path, "rb") as f:
        src = f.read()

    tree = parser_.parse(src)
    dump(tree.root_node, src, max_depth=args.max_depth)


if __name__ == "__main__":
    main()
