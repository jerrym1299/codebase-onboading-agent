"""
Postgres + pgvector persistence for code chunks and embeddings.

Schema is created on startup via init_schema(). Chunks are upserted keyed on
(repo_url, file_path, start_line, chunk_type, name) so re-indexing a repo
overwrites existing rows instead of duplicating them.
"""

import json
import logging
import os
from dataclasses import dataclass

import psycopg
from pgvector.psycopg import register_vector_async
from psycopg_pool import AsyncConnectionPool

from services.chunk_and_embed import CodeChunk
from services.exact_search import RepoTextLine
from services.repo_manifest import RepoFileManifest, RepoManifest

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@postgres:5432/codebase_agent",
)
logger = logging.getLogger(__name__)

CODE_SEARCH_SQL = """
    SELECT file_path, chunk_type, name, start_line, end_line, content,
           1 - (embedding <=> %s::halfvec) AS similarity
    FROM code_chunks
    WHERE repo_url = %s
    ORDER BY embedding <=> %s::halfvec
    LIMIT %s
"""

DIR_SUMMARY_SEARCH_SQL = """
    SELECT dir_path, summary, file_list,
           1 - (embedding <=> %s::halfvec) AS similarity
    FROM dir_summaries
    WHERE repo_url = %s
    ORDER BY embedding <=> %s::halfvec
    LIMIT %s
"""

_pool: AsyncConnectionPool | None = None


async def get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(DATABASE_URL, configure=register_vector_async, open=False)
        await _pool.open()
    return _pool


async def close_pool():
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS code_chunks (
    id BIGSERIAL PRIMARY KEY,
    repo_url TEXT NOT NULL,
    file_path TEXT NOT NULL,
    chunk_type TEXT NOT NULL,
    name TEXT,
    parent_class TEXT,
    start_line INT NOT NULL,
    end_line INT NOT NULL,
    content TEXT NOT NULL,
    chunk_sha256 TEXT,
    embedding_sha256 TEXT,
    embedding_model TEXT,
    embedding halfvec(3072) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT code_chunks_unique UNIQUE NULLS NOT DISTINCT
        (repo_url, file_path, start_line, chunk_type, name)
);

ALTER TABLE IF EXISTS code_chunks
    ADD COLUMN IF NOT EXISTS chunk_sha256 TEXT;

ALTER TABLE IF EXISTS code_chunks
    ADD COLUMN IF NOT EXISTS embedding_sha256 TEXT;

ALTER TABLE IF EXISTS code_chunks
    ADD COLUMN IF NOT EXISTS embedding_model TEXT;

CREATE INDEX IF NOT EXISTS code_chunks_embedding_idx
    ON code_chunks USING hnsw (embedding halfvec_cosine_ops);

CREATE INDEX IF NOT EXISTS code_chunks_repo_idx
    ON code_chunks (repo_url);

CREATE UNIQUE INDEX IF NOT EXISTS code_chunks_repo_chunk_sha_idx
    ON code_chunks (repo_url, chunk_sha256)
    WHERE chunk_sha256 IS NOT NULL;

CREATE INDEX IF NOT EXISTS code_chunks_repo_embedding_sha_idx
    ON code_chunks (repo_url, embedding_sha256);

CREATE TABLE IF NOT EXISTS dir_summaries (
    id BIGSERIAL PRIMARY KEY,
    repo_url TEXT NOT NULL,
    dir_path TEXT NOT NULL,
    summary TEXT NOT NULL,
    file_list TEXT[] NOT NULL DEFAULT '{}',
    embedding halfvec(3072) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT dir_summaries_unique UNIQUE (repo_url, dir_path)
);

CREATE INDEX IF NOT EXISTS dir_summaries_embedding_idx
    ON dir_summaries USING hnsw (embedding halfvec_cosine_ops);

CREATE INDEX IF NOT EXISTS dir_summaries_repo_idx
    ON dir_summaries (repo_url);

CREATE TABLE IF NOT EXISTS repo_index_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_url TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    file_count INT NOT NULL,
    chunk_count INT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS repo_index_runs_repo_idx
    ON repo_index_runs (repo_url, created_at DESC);

CREATE INDEX IF NOT EXISTS repo_index_runs_manifest_idx
    ON repo_index_runs (repo_url, manifest_sha256);

CREATE TABLE IF NOT EXISTS repo_files (
    repo_url TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    language TEXT,
    extension TEXT NOT NULL,
    is_generated BOOLEAN NOT NULL DEFAULT FALSE,
    is_vendor BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (repo_url, file_path)
);

CREATE INDEX IF NOT EXISTS repo_files_sha_idx
    ON repo_files (repo_url, file_sha256);

CREATE INDEX IF NOT EXISTS repo_files_language_idx
    ON repo_files (repo_url, language);

CREATE TABLE IF NOT EXISTS repo_text_lines (
    repo_url TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    language TEXT,
    line_number INT NOT NULL,
    line_text TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (repo_url, file_path, line_number)
);

CREATE INDEX IF NOT EXISTS repo_text_lines_repo_idx
    ON repo_text_lines (repo_url);

CREATE INDEX IF NOT EXISTS repo_text_lines_file_idx
    ON repo_text_lines (repo_url, file_path);

CREATE INDEX IF NOT EXISTS repo_text_lines_language_idx
    ON repo_text_lines (repo_url, language);

CREATE TABLE IF NOT EXISTS repo_chunk_manifests (
    repo_url TEXT NOT NULL,
    chunk_sha256 TEXT NOT NULL,
    embedding_sha256 TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    chunk_type TEXT NOT NULL,
    name TEXT,
    parent_class TEXT,
    start_line INT NOT NULL,
    end_line INT NOT NULL,
    content_bytes INT NOT NULL,
    token_count INT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (repo_url, chunk_sha256)
);

ALTER TABLE IF EXISTS repo_chunk_manifests
    ADD COLUMN IF NOT EXISTS embedding_sha256 TEXT;

ALTER TABLE IF EXISTS repo_chunk_manifests
    ADD COLUMN IF NOT EXISTS embedding_model TEXT;

CREATE INDEX IF NOT EXISTS repo_chunk_manifests_file_idx
    ON repo_chunk_manifests (repo_url, file_path);

CREATE INDEX IF NOT EXISTS repo_chunk_manifests_file_sha_idx
    ON repo_chunk_manifests (repo_url, file_sha256);

CREATE INDEX IF NOT EXISTS repo_chunk_manifests_embedding_sha_idx
    ON repo_chunk_manifests (repo_url, embedding_sha256);

CREATE TABLE IF NOT EXISTS repo_embedding_cache (
    repo_url TEXT NOT NULL,
    embedding_sha256 TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    token_count INT,
    embedding halfvec(3072) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (repo_url, embedding_sha256)
);

CREATE INDEX IF NOT EXISTS repo_embedding_cache_model_idx
    ON repo_embedding_cache (repo_url, embedding_model);

CREATE TABLE IF NOT EXISTS sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_url TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS sessions_repo_idx
    ON sessions (repo_url);

CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    parts JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS messages_session_idx
    ON messages (session_id, created_at);

CREATE TABLE IF NOT EXISTS pending_actions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'resolved', 'cancelled')),
    resolved_value JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS pending_actions_session_idx
    ON pending_actions (session_id, created_at);

CREATE INDEX IF NOT EXISTS pending_actions_session_open_idx
    ON pending_actions (session_id) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS startup_plans (
    repo_url           TEXT PRIMARY KEY,
    plan               JSONB NOT NULL,
    analysis_status    TEXT NOT NULL CHECK (analysis_status IN ('ok', 'partial', 'failed')),
    overall_confidence REAL,
    model              TEXT NOT NULL,
    truncations        TEXT[] NOT NULL DEFAULT '{}',
    error              TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

TRIGRAM_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS repo_text_lines_text_trgm_idx
    ON repo_text_lines USING gin (line_text gin_trgm_ops);
"""


async def init_schema():
    # Create the extension on a raw connection first — the pool's
    # configure() hook calls register_vector_async, which fails if the
    # vector type doesn't exist yet.
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            try:
                await cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            except psycopg.Error as exc:
                logger.warning("Skipping pg_trgm extension setup: %s", exc)

    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(SCHEMA_SQL)

    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(TRIGRAM_INDEX_SQL)
    except psycopg.Error as exc:
        logger.warning("Skipping repo_text_lines trigram index: %s", exc)


async def store_chunks(repo_url: str, chunks: list[CodeChunk], *, replace: bool = False) -> int:
    """Upsert chunks with their embeddings. Returns number of rows written."""
    rows = [
        (
            repo_url,
            c.file_path,
            c.chunk_sha256,
            c.embedding_sha256,
            c.embedding_model,
            c.chunk_type,
            c.name,
            c.parent_class,
            c.start_line,
            c.end_line,
            c.content,
            c.embedding,
        )
        for c in chunks if c.embedding is not None
    ]
    if not rows:
        return 0

    sql = """
        INSERT INTO code_chunks
            (repo_url, file_path, chunk_sha256, embedding_sha256, embedding_model,
             chunk_type, name, parent_class, start_line, end_line, content, embedding)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (repo_url, chunk_sha256) WHERE chunk_sha256 IS NOT NULL
        DO UPDATE SET
            file_path        = EXCLUDED.file_path,
            embedding_sha256 = EXCLUDED.embedding_sha256,
            embedding_model  = EXCLUDED.embedding_model,
            chunk_type       = EXCLUDED.chunk_type,
            name             = EXCLUDED.name,
            parent_class     = EXCLUDED.parent_class,
            start_line       = EXCLUDED.start_line,
            end_line         = EXCLUDED.end_line,
            content          = EXCLUDED.content,
            embedding        = EXCLUDED.embedding
    """

    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(sql, rows)
            if replace:
                chunk_hashes = [c.chunk_sha256 for c in chunks if c.chunk_sha256]
                await cur.execute(
                    """
                    DELETE FROM code_chunks
                    WHERE repo_url = %s
                      AND (
                        chunk_sha256 IS NULL
                        OR NOT (chunk_sha256 = ANY(%s::text[]))
                      )
                    """,
                    (repo_url, chunk_hashes),
                )
    return len(rows)


def _embedding_to_list(value) -> list[float]:
    if isinstance(value, list):
        return value
    if hasattr(value, "to_list"):
        return value.to_list()
    if hasattr(value, "tolist"):
        return value.tolist()
    return list(value)


async def get_cached_chunk_embeddings(
    repo_url: str,
    chunks: list[CodeChunk],
) -> dict[str, list[float]]:
    """Return cached embeddings keyed by embedding_sha256 for the given chunks."""
    keys = sorted({c.embedding_sha256 for c in chunks if c.embedding_sha256})
    if not keys:
        return {}

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT embedding_sha256, embedding
            FROM repo_embedding_cache
            WHERE repo_url = %s
              AND embedding_sha256 = ANY(%s::text[])
            """,
            (repo_url, keys),
        )
        rows = await cur.fetchall()
        if rows:
            await cur.execute(
                """
                UPDATE repo_embedding_cache
                SET last_used_at = NOW()
                WHERE repo_url = %s
                  AND embedding_sha256 = ANY(%s::text[])
                """,
                (repo_url, [row[0] for row in rows]),
            )

    return {row[0]: _embedding_to_list(row[1]) for row in rows}


async def store_cached_chunk_embeddings(repo_url: str, chunks: list[CodeChunk]) -> int:
    """Persist embeddings in a repo-scoped cache keyed by embedding_sha256."""
    rows = [
        (
            repo_url,
            c.embedding_sha256,
            c.embedding_model,
            c.token_count,
            c.embedding,
        )
        for c in chunks
        if c.embedding_sha256 and c.embedding is not None
    ]
    if not rows:
        return 0

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.executemany(
            """
            INSERT INTO repo_embedding_cache
                (repo_url, embedding_sha256, embedding_model, token_count,
                 embedding, last_used_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (repo_url, embedding_sha256) DO UPDATE SET
                embedding_model = EXCLUDED.embedding_model,
                token_count     = EXCLUDED.token_count,
                embedding       = EXCLUDED.embedding,
                last_used_at    = NOW()
            """,
            rows,
        )
    return len(rows)


@dataclass
class DirSummary:
    dir_path: str
    summary: str
    file_list: list[str]
    embedding: list[float] | None = None


async def store_dir_summaries(repo_url: str, summaries: list[DirSummary]) -> int:
    """Upsert directory summaries with embeddings. Returns rows written."""
    rows = [
        (repo_url, s.dir_path, s.summary, s.file_list, s.embedding)
        for s in summaries if s.embedding is not None
    ]
    if not rows:
        return 0

    sql = """
        INSERT INTO dir_summaries
            (repo_url, dir_path, summary, file_list, embedding)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT ON CONSTRAINT dir_summaries_unique
        DO UPDATE SET
            summary   = EXCLUDED.summary,
            file_list = EXCLUDED.file_list,
            embedding = EXCLUDED.embedding
    """

    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(sql, rows)
    return len(rows)


async def store_repo_manifest(
    repo_url: str,
    manifest: RepoManifest,
    metadata: dict | None = None,
) -> dict:
    """Persist the latest content-addressed file/chunk manifest for a repo."""
    file_rows = [
        (
            repo_url,
            f.file_path,
            f.file_sha256,
            f.size_bytes,
            f.language,
            f.extension,
            f.is_generated,
            f.is_vendor,
        )
        for f in manifest.files
    ]
    chunk_rows = [
        (
            repo_url,
            c.chunk_sha256,
            c.embedding_sha256,
            c.embedding_model,
            c.file_path,
            c.file_sha256,
            c.chunk_type,
            c.name,
            c.parent_class,
            c.start_line,
            c.end_line,
            c.content_bytes,
            c.token_count,
        )
        for c in manifest.chunks
    ]

    file_paths = [f.file_path for f in manifest.files]
    chunk_hashes = [c.chunk_sha256 for c in manifest.chunks]

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO repo_index_runs
                (repo_url, manifest_sha256, file_count, chunk_count, metadata)
            VALUES (%s, %s, %s, %s, %s::jsonb)
            RETURNING id, created_at
            """,
            (
                repo_url,
                manifest.manifest_sha256,
                len(manifest.files),
                len(manifest.chunks),
                json.dumps(metadata or {}),
            ),
        )
        run_id, created_at = await cur.fetchone()

        await cur.execute(
            "DELETE FROM repo_files WHERE repo_url = %s AND NOT (file_path = ANY(%s::text[]))",
            (repo_url, file_paths),
        )
        await cur.execute(
            """
            DELETE FROM repo_chunk_manifests
            WHERE repo_url = %s AND NOT (chunk_sha256 = ANY(%s::text[]))
            """,
            (repo_url, chunk_hashes),
        )

        if file_rows:
            await cur.executemany(
                """
                INSERT INTO repo_files
                    (repo_url, file_path, file_sha256, size_bytes, language,
                     extension, is_generated, is_vendor, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (repo_url, file_path) DO UPDATE SET
                    file_sha256 = EXCLUDED.file_sha256,
                    size_bytes   = EXCLUDED.size_bytes,
                    language     = EXCLUDED.language,
                    extension    = EXCLUDED.extension,
                    is_generated = EXCLUDED.is_generated,
                    is_vendor    = EXCLUDED.is_vendor,
                    updated_at   = NOW()
                """,
                file_rows,
            )

        if chunk_rows:
            await cur.executemany(
                """
                INSERT INTO repo_chunk_manifests
                    (repo_url, chunk_sha256, embedding_sha256, embedding_model,
                     file_path, file_sha256, chunk_type, name, parent_class,
                     start_line, end_line, content_bytes, token_count, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (repo_url, chunk_sha256) DO UPDATE SET
                    embedding_sha256 = EXCLUDED.embedding_sha256,
                    embedding_model  = EXCLUDED.embedding_model,
                    file_path        = EXCLUDED.file_path,
                    file_sha256      = EXCLUDED.file_sha256,
                    chunk_type       = EXCLUDED.chunk_type,
                    name             = EXCLUDED.name,
                    parent_class     = EXCLUDED.parent_class,
                    start_line       = EXCLUDED.start_line,
                    end_line         = EXCLUDED.end_line,
                    content_bytes    = EXCLUDED.content_bytes,
                    token_count      = EXCLUDED.token_count,
                    updated_at       = NOW()
                """,
                chunk_rows,
            )

    return {
        "run_id": str(run_id),
        "created_at": created_at.isoformat(),
        "manifest_sha256": manifest.manifest_sha256,
        "file_count": len(manifest.files),
        "chunk_count": len(manifest.chunks),
    }


def _contains_like_pattern(value: str) -> str:
    escaped = (
        value
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


async def store_repo_text_lines(
    repo_url: str,
    lines: list[RepoTextLine],
    files: list[RepoFileManifest],
) -> int:
    """Persist line inventory, rewriting only files whose content hash changed."""
    current_file_paths = sorted({file.file_path for file in files})
    current_sha_by_path = {file.file_path: file.file_sha256 for file in files}

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        if not current_file_paths:
            await cur.execute(
                "DELETE FROM repo_text_lines WHERE repo_url = %s",
                (repo_url,),
            )
            return 0

        await cur.execute(
            """
            DELETE FROM repo_text_lines
            WHERE repo_url = %s
              AND NOT (file_path = ANY(%s::text[]))
            """,
            (repo_url, current_file_paths),
        )
        await cur.execute(
            """
            SELECT file_path, file_sha256
            FROM repo_text_lines
            WHERE repo_url = %s
            GROUP BY file_path, file_sha256
            """,
            (repo_url,),
        )
        existing_sha_by_path = {row[0]: row[1] for row in await cur.fetchall()}
        changed_paths = sorted(
            path for path, sha in current_sha_by_path.items()
            if existing_sha_by_path.get(path) != sha
        )
        changed_path_set = set(changed_paths)

        if changed_paths:
            await cur.execute(
                """
                DELETE FROM repo_text_lines
                WHERE repo_url = %s
                  AND file_path = ANY(%s::text[])
                """,
                (repo_url, changed_paths),
            )

        rows = [
            (
                repo_url,
                line.file_path,
                line.file_sha256,
                line.language,
                line.line_number,
                line.line_text,
            )
            for line in lines
            if line.file_path in changed_path_set
        ]

        if rows:
            await cur.executemany(
                """
                INSERT INTO repo_text_lines
                    (repo_url, file_path, file_sha256, language, line_number,
                     line_text, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                """,
                rows,
            )
    return len(rows)


async def search_repo_text_lines(
    repo_url: str,
    query: str,
    *,
    regex: bool = False,
    path: str = "",
    language: str = "",
    limit: int = 50,
) -> list[dict]:
    """Search the persisted line inventory with literal substring or regex."""
    limit = max(1, min(limit, 200))
    where = ["repo_url = %s"]
    params: list[object] = [repo_url]

    if regex:
        where.append("line_text ~* %s")
        params.append(query)
    else:
        where.append("line_text ILIKE %s ESCAPE '\\'")
        params.append(_contains_like_pattern(query))

    if path:
        where.append("file_path ILIKE %s ESCAPE '\\'")
        params.append(_contains_like_pattern(path))

    if language:
        where.append("language = %s")
        params.append(language)

    params.append(limit)
    sql = f"""
        SELECT file_path, line_number, line_text, language, file_sha256
        FROM repo_text_lines
        WHERE {' AND '.join(where)}
        ORDER BY file_path, line_number
        LIMIT %s
    """

    pool = await get_pool()
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
    except psycopg.errors.InvalidRegularExpression as exc:
        raise ValueError(f"Invalid regex: {query}") from exc

    return [
        {
            "file_path": row[0],
            "line_number": row[1],
            "line_text": row[2],
            "language": row[3],
            "file_sha256": row[4],
        }
        for row in rows
    ]


async def get_latest_repo_manifest_sha(
    repo_url: str,
    *,
    summary_generated: bool | None = None,
) -> str | None:
    summary_filter = ""
    if summary_generated is True:
        summary_filter = "AND metadata->>'summary_generated' = 'true'"
    elif summary_generated is False:
        summary_filter = "AND COALESCE(metadata->>'summary_generated', 'false') = 'false'"

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT manifest_sha256
            FROM repo_index_runs
            WHERE repo_url = %s
              {summary_filter}
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (repo_url,),
        )
        row = await cur.fetchone()
    return row[0] if row else None


STARTUP_PLAN_SELECT_SQL = """
    SELECT plan, analysis_status, overall_confidence, model, truncations, error,
           created_at, updated_at
    FROM startup_plans
    WHERE repo_url = %s
"""


async def get_startup_plan_row(repo_url: str) -> dict | None:
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(STARTUP_PLAN_SELECT_SQL, (repo_url,))
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "plan": row[0],
        "analysis_status": row[1],
        "overall_confidence": row[2],
        "model": row[3],
        "truncations": list(row[4] or []),
        "error": row[5],
        "created_at": row[6].isoformat(),
        "updated_at": row[7].isoformat(),
    }


async def upsert_startup_plan(
    repo_url: str,
    plan: dict,
    analysis_status: str,
    overall_confidence: float | None,
    model: str,
    truncations: list[str],
    error: str | None,
) -> None:
    sql = """
        INSERT INTO startup_plans
            (repo_url, plan, analysis_status, overall_confidence, model,
             truncations, error, updated_at)
        VALUES (%s, %s::jsonb, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (repo_url) DO UPDATE SET
            plan               = EXCLUDED.plan,
            analysis_status    = EXCLUDED.analysis_status,
            overall_confidence = EXCLUDED.overall_confidence,
            model              = EXCLUDED.model,
            truncations        = EXCLUDED.truncations,
            error              = EXCLUDED.error,
            updated_at         = NOW()
    """
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            sql,
            (
                repo_url,
                json.dumps(plan),
                analysis_status,
                overall_confidence,
                model,
                truncations,
                error,
            ),
        )
