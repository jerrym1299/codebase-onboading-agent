"""
Generate natural-language summaries for each directory in a repository
using gpt-4o-mini, then embed them for semantic search.
"""

import os
from collections import defaultdict

from openai import OpenAI
from services.db import DirSummary

client = OpenAI()

MAX_SNIPPET_LINES = 40
MAX_CONTEXT_CHARS = 12_000


def _build_dir_context(dir_path: str, file_paths: list[str]) -> str:
    """Build a compact context string for one directory: file list + leading lines."""
    parts: list[str] = [f"Directory: {dir_path}", f"Files ({len(file_paths)}):"]
    total_chars = 0

    for fp in sorted(file_paths):
        fname = os.path.basename(fp)
        parts.append(f"\n--- {fname} ---")
        try:
            with open(fp, "r", errors="replace") as f:
                lines = f.readlines()[:MAX_SNIPPET_LINES]
            snippet = "".join(lines).rstrip()
        except OSError:
            snippet = "(unreadable)"
        parts.append(snippet)
        total_chars += len(snippet)
        if total_chars > MAX_CONTEXT_CHARS:
            parts.append("\n... (remaining files omitted for brevity)")
            break

    return "\n".join(parts)


def _group_by_directory(file_paths: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for fp in file_paths:
        groups[os.path.dirname(fp)].append(fp)
    return dict(groups)


SYSTEM_PROMPT = (
    "You are a senior engineer summarising a directory inside a codebase. "
    "Given the directory path, its file list, and leading lines of each file, "
    "write a concise 2-5 sentence summary covering:\n"
    "- The purpose / responsibility of this directory\n"
    "- Key modules, classes, or exports it contains\n"
    "- How it relates to the rest of the project (if apparent)\n"
    "Be specific — mention actual names. Do not repeat the file list verbatim."
)


def generate_dir_summaries(
    file_paths: list[str],
    repo_dir: str,
) -> list[DirSummary]:
    """
    Group files by directory, call gpt-4o-mini to summarise each one,
    embed the summaries, and return DirSummary objects ready for storage.
    """
    groups = _group_by_directory(file_paths)
    summaries: list[DirSummary] = []

    for dir_path, files in groups.items():
        rel_dir = os.path.relpath(dir_path, repo_dir) if dir_path != repo_dir else "."
        context = _build_dir_context(dir_path, files)

        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": context},
            ],
            temperature=0.3,
            max_tokens=300,
        )
        summary_text = resp.choices[0].message.content.strip()

        summaries.append(DirSummary(
            dir_path=rel_dir,
            summary=summary_text,
            file_list=[os.path.basename(f) for f in files],
        ))

    # Batch-embed all summaries
    if summaries:
        texts = [
            f"Directory: {s.dir_path}\n{s.summary}"
            for s in summaries
        ]
        BATCH = 100
        for i in range(0, len(texts), BATCH):
            batch = texts[i:i + BATCH]
            resp = client.embeddings.create(
                input=batch, model="text-embedding-3-small"
            )
            for s, datum in zip(summaries[i:i + BATCH], resp.data):
                s.embedding = datum.embedding

    return summaries
