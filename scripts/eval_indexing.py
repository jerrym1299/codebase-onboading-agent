"""
eval_indexing.py - local quality gate for content-addressed indexing.

Checks the manifest path, DB persistence, and embedding-cache behavior without
requiring a real OpenAI key. The cache smoke runs inside the fastapi container
and monkeypatches embedding generation to deterministic vectors. It also checks
the persisted exact line-search inventory.

Usage:
    python3 scripts/eval_indexing.py
    python3 scripts/eval_indexing.py --base http://localhost:8001
    python3 scripts/eval_indexing.py --with-openai
    python3 scripts/eval_indexing.py --skip-cache-smoke
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_BASE = "http://localhost:8001"
DEFAULT_REPO = "https://github.com/octocat/Spoon-Knife"
DEFAULT_EXPECTED_FILES = "README.md,index.html,styles.css"
DEFAULT_EXACT_QUERY = "Well hello there!"
DEFAULT_EXACT_EXPECTED_PATH = "README.md"
ROOT = Path(__file__).resolve().parents[1]


def _http_json(url: str, timeout: int = 60) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def _run(cmd: list[str], *, timeout: int = 120) -> str:
    result = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(cmd)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result.stdout.strip()


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _psql(sql: str) -> str:
    return _run([
        "docker", "compose", "exec", "-T", "postgres",
        "psql", "-U", "postgres", "-d", "codebase_agent",
        "-At", "-c", sql,
    ])


def _assert_hash(value: str, label: str) -> None:
    assert isinstance(value, str), f"{label} is not a string"
    assert len(value) == 64, f"{label} should be sha256 length, got {len(value)}"
    int(value, 16)


def _fetch_manifest(base: str, repo: str) -> dict:
    encoded_repo = urllib.parse.quote(repo, safe="")
    return _http_json(f"{base}/manifest?repo_url={encoded_repo}&limit=1000", timeout=120)


def _fetch_chunks(base: str, repo: str) -> dict:
    encoded_repo = urllib.parse.quote(repo, safe="")
    return _http_json(f"{base}/chunks?repo_url={encoded_repo}&preview=0", timeout=300)


def _fetch_exact(base: str, repo: str, query: str, limit: int = 20) -> dict:
    encoded_repo = urllib.parse.quote(repo, safe="")
    encoded_query = query.replace(" ", "+")
    return _http_json(
        f"{base}/search-exact?repo_url={encoded_repo}&query={encoded_query}&limit={limit}",
        timeout=120,
    )


def _validate_manifest(manifest: dict, expected_files: set[str]) -> None:
    assert manifest["file_count"] > 0, f"expected files, got {manifest}"
    assert manifest["chunk_count"] > 0, f"expected chunks, got {manifest}"
    _assert_hash(manifest["manifest_sha256"], "manifest_sha256")

    files = manifest.get("files") or []
    chunks = manifest.get("chunks") or []
    assert len(files) == manifest["file_count"], "manifest response truncated files"
    assert len(chunks) == manifest["chunk_count"], "manifest response truncated chunks"

    file_paths = {row["file_path"] for row in files}
    missing = expected_files - file_paths
    assert not missing, f"missing expected files: {sorted(missing)}"

    for row in files:
        assert not row["file_path"].startswith("/"), f"absolute file path leaked: {row}"
        _assert_hash(row["file_sha256"], f"file_sha256:{row['file_path']}")
        assert row["size_bytes"] >= 0, f"negative size: {row}"

    for row in chunks:
        assert row["file_path"] in file_paths, f"chunk file missing from files: {row}"
        _assert_hash(row["chunk_sha256"], f"chunk_sha256:{row['file_path']}")
        _assert_hash(row["embedding_sha256"], f"embedding_sha256:{row['file_path']}")
        assert row["embedding_model"] == "text-embedding-3-large", row
        assert row["token_count"] > 0, row


def _validate_db(repo: str, manifest: dict) -> None:
    repo_sql = _sql_literal(repo)
    files = int(_psql(f"SELECT count(*) FROM repo_files WHERE repo_url={repo_sql};"))
    chunks = int(_psql(f"SELECT count(*) FROM repo_chunk_manifests WHERE repo_url={repo_sql};"))
    text_lines = int(_psql(f"SELECT count(*) FROM repo_text_lines WHERE repo_url={repo_sql};"))
    runs = int(_psql(f"SELECT count(*) FROM repo_index_runs WHERE repo_url={repo_sql};"))
    connections = int(_psql(f"SELECT count(*) FROM repo_connections WHERE repo_url={repo_sql};"))
    indexes = int(_psql(f"SELECT count(*) FROM repo_indexes WHERE repo_url={repo_sql};"))
    indexed_runs = int(_psql(
        f"SELECT count(*) FROM repo_index_runs WHERE repo_url={repo_sql} "
        "AND repo_index_id IS NOT NULL;"
    ))
    latest = _psql(
        "SELECT manifest_sha256 FROM repo_index_runs "
        f"WHERE repo_url={repo_sql} ORDER BY created_at DESC LIMIT 1;"
    )
    latest_index = _psql(
        "SELECT ri.manifest_sha256 "
        "FROM repo_latest_indexes li "
        "JOIN repo_indexes ri ON ri.id = li.repo_index_id "
        "JOIN repo_connections rc ON rc.id = li.repo_connection_id "
        f"WHERE rc.repo_url={repo_sql} "
        "ORDER BY li.updated_at DESC LIMIT 1;"
    )

    assert files == manifest["file_count"], f"repo_files={files}, manifest={manifest['file_count']}"
    assert chunks == manifest["chunk_count"], (
        f"repo_chunk_manifests={chunks}, manifest={manifest['chunk_count']}"
    )
    assert text_lines == manifest["text_line_count"], (
        f"repo_text_lines={text_lines}, manifest={manifest['text_line_count']}"
    )
    assert runs >= 2, f"expected at least two manifest runs, got {runs}"
    assert latest == manifest["manifest_sha256"], "latest manifest hash mismatch"
    assert connections >= 1, "expected repo_connection for indexed repo"
    assert indexes >= 1, "expected repo_index for indexed repo"
    assert indexed_runs >= 1, "expected repo_index_runs.repo_index_id to be populated"
    assert latest_index == manifest["manifest_sha256"], "latest repo_index hash mismatch"


def _validate_exact_search(
    base: str,
    repo: str,
    query: str,
    expected_path: str,
) -> dict:
    result = _fetch_exact(base, repo, query)
    rows = result.get("results") or []
    assert rows, f"expected exact-search results for {query!r}"
    result_paths = {row["file_path"] for row in rows}
    assert expected_path in result_paths, (
        f"{expected_path} not in exact-search results: {result}"
    )
    for row in rows:
        assert row["line_number"] > 0, row
        assert query.lower() in row["line_text"].lower(), row
    return result


def _clear_cache_rows(repo: str) -> None:
    repo_sql = _sql_literal(repo)
    _psql(f"DELETE FROM repo_embedding_cache WHERE repo_url={repo_sql};")
    _psql(f"DELETE FROM code_chunks WHERE repo_url={repo_sql};")


def _validate_container_openai_key() -> None:
    result = _run([
        "docker", "compose", "exec", "-T", "fastapi",
        "sh", "-lc",
        "test -n \"$OPENAI_API_KEY\" && printf present || printf missing",
    ])
    assert result == "present", (
        "fastapi container is missing OPENAI_API_KEY. "
        "Restart it with the key before running --with-openai."
    )


def _cache_smoke(repo: str) -> dict:
    snippet = f"""
import asyncio
import json

from services.clone_repo import ensure_repo_dir
from services.walk_repo import collect_file_paths
from services.chunk_and_embed import chunk_file_list
from services.repo_manifest import build_repo_manifest
from services.db import close_pool, store_chunks
import services.embedding_cache as embedding_cache

REPO_URL = {repo!r}

def fake_embed_chunks(chunks):
    for index, chunk in enumerate(chunks):
        chunk.embedding = [float(index + 1) / 1000.0] * 3072
    return len(chunks)

async def index_once():
    repo_dir = await ensure_repo_dir(REPO_URL)
    paths = await collect_file_paths(repo_dir)
    chunks = chunk_file_list(paths, embed=False)
    build_repo_manifest(repo_dir, paths, chunks)
    stats = await embedding_cache.hydrate_embeddings(REPO_URL, chunks)
    await store_chunks(REPO_URL, chunks, replace=True)
    return {{"files": len(paths), "chunks": len(chunks), "stats": stats}}

async def main():
    embedding_cache.embed_chunks = fake_embed_chunks
    first = await index_once()
    second = await index_once()
    print(json.dumps({{"first": first, "second": second}}))
    await close_pool()

asyncio.run(main())
"""
    raw = _run([
        "docker", "compose", "exec", "-T", "fastapi",
        "python", "-c", snippet,
    ], timeout=180)
    return json.loads(raw.splitlines()[-1])


def _validate_cache_smoke(repo: str, manifest: dict) -> dict:
    _clear_cache_rows(repo)
    result = _cache_smoke(repo)
    first = result["first"]["stats"]
    second = result["second"]["stats"]
    expected_chunks = manifest["chunk_count"]

    assert first["total"] == expected_chunks, first
    assert first["cached"] == 0, first
    assert first["embedded"] == expected_chunks, first
    assert first["stored"] == expected_chunks, first

    assert second["total"] == expected_chunks, second
    assert second["cached"] == expected_chunks, second
    assert second["embedded"] == 0, second
    assert second["stored"] == 0, second
    return result


def _validate_real_openai(base: str, repo: str, manifest: dict) -> dict:
    _validate_container_openai_key()
    _clear_cache_rows(repo)

    first = _fetch_chunks(base, repo)
    assert first["file_count"] == manifest["file_count"], first
    assert first["chunk_count"] == manifest["chunk_count"], first
    assert first["stored"] == manifest["chunk_count"], first
    assert first["embeddings"]["cached"] == 0, first["embeddings"]
    assert first["embeddings"]["embedded"] == manifest["chunk_count"], first["embeddings"]
    assert first["embeddings"]["stored"] == manifest["chunk_count"], first["embeddings"]

    for row in first["chunks"]:
        embedding = row.get("embedding")
        assert isinstance(embedding, list), f"missing embedding: {row}"
        assert len(embedding) == 3072, f"unexpected embedding dim: {len(embedding)}"

    second = _fetch_chunks(base, repo)
    assert second["embeddings"]["cached"] == manifest["chunk_count"], second["embeddings"]
    assert second["embeddings"]["embedded"] == 0, second["embeddings"]
    assert second["embeddings"]["stored"] == 0, second["embeddings"]

    repo_sql = _sql_literal(repo)
    cached = int(_psql(
        f"SELECT count(*) FROM repo_embedding_cache WHERE repo_url={repo_sql};"
    ))
    indexed = int(_psql(
        f"SELECT count(*) FROM code_chunks WHERE repo_url={repo_sql};"
    ))
    assert cached == manifest["chunk_count"], f"cached embeddings={cached}"
    assert indexed == manifest["chunk_count"], f"code chunks={indexed}"

    search = _http_json(
        f"{base}/search?repo_url={urllib.parse.quote(repo, safe='')}&query=stylesheet&k=3",
        timeout=120,
    )
    result_paths = {row["file_path"].split("/")[-1] for row in search.get("results", [])}
    assert "styles.css" in result_paths, f"styles.css not in search results: {search}"

    return {
        "first": first["embeddings"],
        "second": second["embeddings"],
        "search_paths": sorted(result_paths),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--expected-files", default=DEFAULT_EXPECTED_FILES)
    parser.add_argument("--exact-query", default=DEFAULT_EXACT_QUERY)
    parser.add_argument("--exact-expected-path", default=DEFAULT_EXACT_EXPECTED_PATH)
    parser.add_argument("--skip-db", action="store_true")
    parser.add_argument("--skip-exact", action="store_true")
    parser.add_argument("--skip-cache-smoke", action="store_true")
    parser.add_argument(
        "--with-openai",
        action="store_true",
        help="Run real OpenAI embedding and vector-search checks.",
    )
    args = parser.parse_args()

    expected_files = {
        item.strip() for item in args.expected_files.split(",") if item.strip()
    }

    print(f"Checking API at {args.base} ...")
    health = _http_json(f"{args.base}/")
    assert health == {"Hello": "world"}, f"unexpected health response: {health}"

    print(f"Fetching manifest for {args.repo} twice ...")
    first_manifest = _fetch_manifest(args.base, args.repo)
    second_manifest = _fetch_manifest(args.base, args.repo)
    _validate_manifest(first_manifest, expected_files)
    _validate_manifest(second_manifest, expected_files)
    assert first_manifest["manifest_sha256"] == second_manifest["manifest_sha256"], (
        "manifest hash changed across identical runs"
    )
    print(
        "  manifest stable: "
        f"files={second_manifest['file_count']} chunks={second_manifest['chunk_count']} "
        f"sha={second_manifest['manifest_sha256'][:12]}"
    )

    if not args.skip_db:
        print("Checking persisted manifest tables ...")
        _validate_db(args.repo, second_manifest)
        print("  DB counts match manifest")

    if not args.skip_exact:
        print("Checking exact line search ...")
        result = _validate_exact_search(
            args.base,
            args.repo,
            args.exact_query,
            args.exact_expected_path,
        )
        print(f"  exact search returned {len(result['results'])} match(es)")

    if not args.skip_cache_smoke:
        print("Checking embedding cache behavior with deterministic fake embeddings ...")
        result = _validate_cache_smoke(args.repo, second_manifest)
        print(
            "  cache smoke: "
            f"first={result['first']['stats']} second={result['second']['stats']}"
        )

    if args.with_openai:
        print("Checking real OpenAI embeddings and vector search ...")
        result = _validate_real_openai(args.base, args.repo, second_manifest)
        print(
            "  OpenAI smoke: "
            f"first={result['first']} second={result['second']} "
            f"search_paths={result['search_paths']}"
        )

    print("\nALL INDEXING EVALS PASSED.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nINDEXING EVAL FAILED: {exc}", file=sys.stderr)
        raise
