"""
Generate natural-language summaries for each directory in a repository
using gpt-5.4 over the already-extracted code chunks, then embed them
for semantic search.
"""

import logging
import os
from collections import defaultdict
import asyncio
import tiktoken
from openai import AsyncOpenAI

from services.chunk_and_embed import CodeChunk
from services.db import DirSummary

client = AsyncOpenAI()
logger = logging.getLogger(__name__)

MAX_INPUT_TOKENS = 800_000
MAX_OUTPUT_TOKENS = 10_000

# o200k_base is the encoding family used since gpt-4o; safe default
# while gpt-5.4 may not yet be registered in tiktoken's model table.
_encoder = tiktoken.get_encoding("o200k_base")


def _format_chunk(chunk: CodeChunk) -> str:
    header = f"[{chunk.chunk_type}"
    if chunk.name:
        header += f": {chunk.name}"
    if chunk.parent_class:
        header += f" (in {chunk.parent_class})"
    header += f"] L{chunk.start_line}-{chunk.end_line}"
    return f"{header}\n{chunk.content}"


def _build_dir_context(dir_path: str, dir_chunks: list[CodeChunk]) -> str:
    """Build a token-bounded context string for one directory by walking the
    chunks file-by-file, sorted by line, and stopping when the budget is hit."""
    by_file: dict[str, list[CodeChunk]] = defaultdict(list)
    for c in dir_chunks:
        by_file[c.file_path].append(c)
    for cs in by_file.values():
        cs.sort(key=lambda c: c.start_line)

    parts: list[str] = [f"Directory: {dir_path}", f"Files ({len(by_file)}):"]
    used = len(_encoder.encode("\n".join(parts)))

    truncated = False
    for fp in sorted(by_file.keys()):
        block = f"\n=== File: {os.path.basename(fp)} ===\n" + "\n\n".join(
            _format_chunk(c) for c in by_file[fp]
        )
        block_tokens = len(_encoder.encode(block))
        if used + block_tokens > MAX_INPUT_TOKENS:
            truncated = True
            break
        parts.append(block)
        used += block_tokens

    if truncated:
        parts.append(
            "\n... (remaining files omitted: directory exceeds input token budget)"
        )
    return "\n".join(parts)


def _group_by_directory(chunks: list[CodeChunk]) -> dict[str, list[CodeChunk]]:
    groups: dict[str, list[CodeChunk]] = defaultdict(list)
    for c in chunks:
        groups[os.path.dirname(c.file_path)].append(c)
    return dict(groups)


SYSTEM_PROMPT = (
    "You are a senior engineer writing a directory-level summary for a "
    "codebase-onboarding agent. You receive the directory path, its file "
    "list, and the full set of extracted code chunks (with chunk type, "
    "name, parent class, line range, and content) for each file.\n\n"
    "Cover, succinctly but thoroughly:\n"
    "- The purpose and responsibility of this directory.\n"
    "- Key modules, classes, functions, and what they do.\n"
    "- How the pieces compose — data flow, control flow, important "
    "dependencies between files in this directory.\n"
    "- How this directory relates to the rest of the project, if apparent.\n"
    "- Notable patterns, conventions, invariants, or non-obvious behaviour.\n\n"
    "Reference actual names. Match length to complexity: a trivial utils "
    "directory can be a single paragraph; a complex orchestration directory "
    "may warrant several paragraphs. Prefer prose over bullet lists. "
    "No fluff, no boilerplate, no restating the file list."
)

SEM = asyncio.Semaphore(16)


async def summarise_one(
    dir_path: str, dir_chunks: list[CodeChunk], repo_dir: str,
) -> DirSummary:
    context = _build_dir_context(dir_path, dir_chunks)
    async with SEM:
        resp = await client.chat.completions.create(
            model="gpt-5.4",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": context},
            ],
            max_completion_tokens=MAX_OUTPUT_TOKENS,
        )
    summary_text = resp.choices[0].message.content.strip()
    files_in_dir = sorted({os.path.basename(c.file_path) for c in dir_chunks})
    rel_dir = os.path.relpath(dir_path, repo_dir) if dir_path != repo_dir else "."
    return DirSummary(dir_path=rel_dir, summary=summary_text, file_list=files_in_dir)


async def generate_dir_summaries(
    chunks: list[CodeChunk],
    repo_dir: str,
) -> list[DirSummary]:
    """Group chunks by directory, summarise each with gpt-5.4, embed the
    summaries with text-embedding-3-large, and return DirSummary rows."""
    groups = _group_by_directory(chunks)

    results = await asyncio.gather(
        *[summarise_one(d, c, repo_dir) for d, c in groups.items()],
        return_exceptions=True,
    )

    summaries: list[DirSummary] = []
    for (dir_path, _), r in zip(groups.items(), results):
        if isinstance(r, DirSummary):
            summaries.append(r)
        else:
            logger.warning("dir summary failed for %s: %s", dir_path, r)

    if summaries:
        texts = [f"Directory: {s.dir_path}\n{s.summary}" for s in summaries]
        BATCH = 100
        for i in range(0, len(texts), BATCH):
            batch = texts[i:i + BATCH]
            resp = await client.embeddings.create(
                input=batch, model="text-embedding-3-large",
            )
            for s, datum in zip(summaries[i:i + BATCH], resp.data):
                s.embedding = datum.embedding

    return summaries
