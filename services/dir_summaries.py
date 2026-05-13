"""
Generate natural-language summaries for each directory in a repository
using gpt-5.4 over the already-extracted code chunks, then embed them
for semantic search.
"""

import logging
import os
from collections import defaultdict
from typing import Awaitable, Callable
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


ProgressCallback = Callable[[int, int, str], Awaitable[None]]


async def _summarise_with_label(
    dir_path: str, dir_chunks: list[CodeChunk], repo_dir: str,
) -> tuple[str, DirSummary | BaseException]:
    try:
        return dir_path, await summarise_one(dir_path, dir_chunks, repo_dir)
    except BaseException as exc:  # noqa: BLE001
        return dir_path, exc


async def generate_dir_summaries(
    chunks: list[CodeChunk],
    repo_dir: str,
    on_progress: ProgressCallback | None = None,
) -> list[DirSummary]:
    """Group chunks by directory, summarise each with gpt-5.4, embed the
    summaries with text-embedding-3-large, and return DirSummary rows.

    If on_progress is supplied, it is awaited after each per-directory
    summary completes with (done, total, dir_path)."""
    groups = _group_by_directory(chunks)
    total = len(groups)

    tasks = [
        asyncio.create_task(_summarise_with_label(d, c, repo_dir))
        for d, c in groups.items()
    ]

    summaries: list[DirSummary] = []
    done = 0
    for fut in asyncio.as_completed(tasks):
        dir_path, result = await fut
        done += 1
        if isinstance(result, DirSummary):
            summaries.append(result)
        else:
            logger.warning("dir summary failed for %s: %s", dir_path, result)
        if on_progress is not None:
            try:
                await on_progress(done, total, dir_path)
            except Exception:  # noqa: BLE001
                logger.exception("on_progress callback raised; continuing")

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
