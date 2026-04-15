"""
show_ast.py — print the tree-sitter AST for a source file.

Usage:
    python3 scripts/show_ast.py <file> [--max-depth N]

Supported extensions: .py .js .jsx .ts .tsx
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.chunk_and_embed import AST_PARSERS, dump_ast


def main() -> None:
    ap = argparse.ArgumentParser(description="Print the tree-sitter AST for a file.")
    ap.add_argument("path", help="path to a .py/.js/.jsx/.ts/.tsx file")
    ap.add_argument("--max-depth", type=int, default=None, help="limit recursion depth")
    args = ap.parse_args()

    parser_ = AST_PARSERS.get(os.path.splitext(args.path)[1].lower())
    if parser_ is None:
        sys.exit(f"No tree-sitter parser for {args.path!r}. Supported: {sorted(AST_PARSERS)}")

    with open(args.path, "rb") as f:
        src = f.read()

    for line in dump_ast(parser_.parse(src).root_node, src, max_depth=args.max_depth):
        print(line)


if __name__ == "__main__":
    main()
