"""
Postgres + pgvector persistence for code chunks and embeddings.

Schema is created on startup via init_schema(). Chunks are upserted keyed on
(repo_url, file_path, start_line, chunk_type, name) so re-indexing a repo
overwrites existing rows instead of duplicating them.
"""

import json
import os
from dataclasses import dataclass

import psycopg
from pgvector.psycopg import register_vector_async
from psycopg_pool import AsyncConnectionPool

from services.chunk_and_embed import CodeChunk

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@postgres:5432/codebase_agent",
)

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
    embedding halfvec(3072) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT code_chunks_unique UNIQUE NULLS NOT DISTINCT
        (repo_url, file_path, start_line, chunk_type, name)
);

CREATE INDEX IF NOT EXISTS code_chunks_embedding_idx
    ON code_chunks USING hnsw (embedding halfvec_cosine_ops);

CREATE INDEX IF NOT EXISTS code_chunks_repo_idx
    ON code_chunks (repo_url);

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


async def init_schema():
    # Create the extension on a raw connection first — the pool's
    # configure() hook calls register_vector_async, which fails if the
    # vector type doesn't exist yet.
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute("CREATE EXTENSION IF NOT EXISTS vector")

    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(SCHEMA_SQL)


async def store_chunks(repo_url: str, chunks: list[CodeChunk]) -> int:
    """Upsert chunks with their embeddings. Returns number of rows written."""
    rows = [
        (
            repo_url,
            c.file_path,
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
            (repo_url, file_path, chunk_type, name, parent_class,
             start_line, end_line, content, embedding)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT ON CONSTRAINT code_chunks_unique
        DO UPDATE SET
            parent_class = EXCLUDED.parent_class,
            end_line     = EXCLUDED.end_line,
            content      = EXCLUDED.content,
            embedding    = EXCLUDED.embedding
    """

    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(sql, rows)
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
