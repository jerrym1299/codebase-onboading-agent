"""
show_chunks.py — print the semantic chunks a file would be split into.

Usage:
    python3 scripts/show_chunks.py <file> [<file> ...] [--full] [--preview N]

Supported extensions: .py .js .jsx .ts .tsx .md .markdown
                      .json .yaml .yml .css .html .htm .sh
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.chunk_and_embed import chunk_file_list


def main() -> None:
    ap = argparse.ArgumentParser(description="Print the chunks produced for each file.")
    ap.add_argument("paths", nargs="+", help="one or more source file paths")
    ap.add_argument("--full", action="store_true", help="print full chunk content")
    ap.add_argument("--preview", type=int, default=500,
                    help="chars of content to preview when not --full (default 500)")
    args = ap.parse_args()

    chunks = chunk_file_list(args.paths)

    print(f"# {len(chunks)} chunk(s) across {len(args.paths)} file(s)\n")
    for i, c in enumerate(chunks):
        header = (
            f"=== [{i}] {c.chunk_type}  name={c.name}  "
            f"file={c.file_path}  lines={c.start_line}-{c.end_line}  "
            f"tokens={c.token_count}"
        )
        if c.parent_class:
            header += f"  parent_class={c.parent_class}"
        print(header + " ===")

        text = c.embedding_text
        if args.full or len(text) <= args.preview:
            print(text)
        else:
            print(text[:args.preview] + f"\n... [truncated, {len(text) - args.preview} more chars]")
        print()


if __name__ == "__main__":
    main()
