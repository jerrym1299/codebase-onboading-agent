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


SESSION_MIGRATION_SQLS = (
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        status TEXT NOT NULL,
        app_plan_hash TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS app_plan_hash TEXT",
    "ALTER TABLE sessions DROP COLUMN IF EXISTS repo_url",
    """ALTER TABLE app_startup_plans
      ADD COLUMN IF NOT EXISTS verification_status TEXT
      CHECK (verification_status IN ('not_started','running','passed','blocked','failed'))
      DEFAULT 'not_started';""",
    """ALTER TABLE app_startup_plans
      ADD COLUMN IF NOT EXISTS verification JSONB NOT NULL DEFAULT '{}'::jsonb;""",
)


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
    status TEXT NOT NULL,
    app_plan_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS sessions_app_plan_hash_idx
    ON sessions (app_plan_hash);

CREATE TABLE IF NOT EXISTS session_repos (
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    repo_url   TEXT NOT NULL,
    PRIMARY KEY (session_id, repo_url)
);

CREATE INDEX IF NOT EXISTS session_repos_repo_idx
    ON session_repos (repo_url);

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

CREATE TABLE IF NOT EXISTS repo_boundaries (
    repo_url           TEXT PRIMARY KEY,
    report             JSONB NOT NULL,
    analysis_status    TEXT NOT NULL CHECK (analysis_status IN ('ok', 'partial', 'failed')),
    model              TEXT NOT NULL,
    error              TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS app_startup_plans (
    repo_set_hash          TEXT PRIMARY KEY,
    repo_urls              TEXT[] NOT NULL,
    plan_markdown          TEXT NOT NULL,
    graph                  JSONB NOT NULL,
    ambiguities            JSONB NOT NULL DEFAULT '[]',
    orchestration_findings JSONB NOT NULL DEFAULT '[]',
    analysis_status        TEXT NOT NULL CHECK (analysis_status IN ('ok', 'partial', 'failed')),
    model                  TEXT NOT NULL,
    error                  TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
            for sql in SESSION_MIGRATION_SQLS:
                await cur.execute(sql)


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


async def get_session_repo_urls(session_id: str) -> list[str]:
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT repo_url FROM session_repos WHERE session_id = %s ORDER BY repo_url",
            (session_id,),
        )
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def insert_session_repos(session_id: str, repo_urls: list[str]) -> None:
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO session_repos (session_id, repo_url) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            [(session_id, u) for u in repo_urls],
        )


REPO_BOUNDARIES_SELECT_SQL = """
    SELECT report, analysis_status, model, error, created_at, updated_at
    FROM repo_boundaries
    WHERE repo_url = %s
"""


async def get_repo_boundaries_row(repo_url: str) -> dict | None:
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(REPO_BOUNDARIES_SELECT_SQL, (repo_url,))
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "report": row[0],
        "analysis_status": row[1],
        "model": row[2],
        "error": row[3],
        "created_at": row[4].isoformat(),
        "updated_at": row[5].isoformat(),
    }

DIR_SUMMARIES_SELECT_SQL = """
    SELECT dir_path, summary, file_list, created_at
    FROM dir_summaries
    WHERE repo_url = %s
    ORDER BY dir_path
"""

async def get_dir_summaries_for_repo(repo_url: str) -> list[dict]:
    """Return all directory summaries for a repo, ordered by dir_path.
    Returns an empty list if the repo has no summaries yet."""
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(DIR_SUMMARIES_SELECT_SQL, (repo_url,))
        rows = await cur.fetchall()
    return [
        {
            "dir_path": r[0],
            "summary": r[1],
            "file_list": list(r[2] or []),
            "created_at": r[3].isoformat(),
        }
        for r in rows
    ]


async def upsert_repo_boundaries(
    repo_url: str,
    report: dict,
    analysis_status: str,
    model: str,
    error: str | None,
) -> None:
    sql = """
        INSERT INTO repo_boundaries
            (repo_url, report, analysis_status, model, error, updated_at)
        VALUES (%s, %s::jsonb, %s, %s, %s, NOW())
        ON CONFLICT (repo_url) DO UPDATE SET
            report          = EXCLUDED.report,
            analysis_status = EXCLUDED.analysis_status,
            model           = EXCLUDED.model,
            error           = EXCLUDED.error,
            updated_at      = NOW()
    """
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            sql,
            (repo_url, json.dumps(report), analysis_status, model, error),
        )


APP_STARTUP_PLAN_SELECT_SQL = """
    SELECT repo_set_hash, repo_urls, plan_markdown, graph, ambiguities,
           orchestration_findings, analysis_status, model, error,
           created_at, updated_at, verification_status, verification
    FROM app_startup_plans
    WHERE repo_set_hash = %s
"""


async def get_app_startup_plan_row(repo_set_hash: str) -> dict | None:
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(APP_STARTUP_PLAN_SELECT_SQL, (repo_set_hash,))
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "repo_set_hash": row[0],
        "repo_urls": list(row[1] or []),
        "plan_markdown": row[2],
        "graph": row[3],
        "ambiguities": row[4],
        "orchestration_findings": row[5],
        "analysis_status": row[6],
        "model": row[7],
        "error": row[8],
        "created_at": row[9].isoformat(),
        "updated_at": row[10].isoformat(),
        "verification_status": row[11],
        "verification": row[12],
    }


async def upsert_app_startup_plan(
    repo_set_hash: str,
    repo_urls: list[str],
    plan_markdown: str,
    graph: dict,
    ambiguities: list,
    orchestration_findings: list,
    analysis_status: str,
    model: str,
    error: str | None,
    verification_status: str = "not_started",
    verification: dict | None = None,
) -> None:
    if verification is None:
        verification = {}
    sql = """
        INSERT INTO app_startup_plans
            (repo_set_hash, repo_urls, plan_markdown, graph, ambiguities,
             orchestration_findings, analysis_status, model, error,
             verification_status, verification, updated_at)
        VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s,
                %s, %s::jsonb, NOW())
        ON CONFLICT (repo_set_hash) DO UPDATE SET
            repo_urls              = EXCLUDED.repo_urls,
            plan_markdown          = EXCLUDED.plan_markdown,
            graph                  = EXCLUDED.graph,
            ambiguities            = EXCLUDED.ambiguities,
            orchestration_findings = EXCLUDED.orchestration_findings,
            analysis_status        = EXCLUDED.analysis_status,
            model                  = EXCLUDED.model,
            error                  = EXCLUDED.error,
            verification_status    = EXCLUDED.verification_status,
            verification           = EXCLUDED.verification,
            updated_at             = NOW()
    """
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            sql,
            (
                repo_set_hash,
                repo_urls,
                plan_markdown,
                json.dumps(graph),
                json.dumps(ambiguities),
                json.dumps(orchestration_findings),
                analysis_status,
                model,
                error,
                verification_status,
                json.dumps(verification),
            ),
        )


async def update_app_startup_plan_verification(
    repo_set_hash: str,
    verification_status: str,
    verification: dict,
) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            """UPDATE app_startup_plans
               SET verification_status = %s,
                   verification = %s::jsonb,
                   updated_at = NOW()
               WHERE repo_set_hash = %s""",
            (verification_status, json.dumps(verification), repo_set_hash),
        )
