"""
Postgres + pgvector persistence for code chunks and embeddings.

Schema is created on startup via init_schema(). The legacy serving tables remain
repo_url keyed for compatibility, while Phase 1 tenant/repo/index/job tables add
durable ownership, versioning, and worker status around those rows.
"""

import json
import logging
import os
from dataclasses import dataclass
from urllib.parse import quote_plus, urlparse

import psycopg
from pgvector.psycopg import register_vector_async
from psycopg_pool import AsyncConnectionPool

from services.chunk_and_embed import CodeChunk, EMBEDDING_MODEL
from services.exact_search import RepoTextLine
from services.repo_manifest import RepoFileManifest, RepoManifest

def _database_url_from_env() -> str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]

    host = os.environ.get("DATABASE_HOST")
    password = os.environ.get("DATABASE_PASSWORD")
    if host and password:
        user = os.environ.get("DATABASE_USER", "postgres")
        port = os.environ.get("DATABASE_PORT", "5432")
        name = os.environ.get("DATABASE_NAME", "codebase_agent")
        return (
            f"postgresql://{quote_plus(user)}:{quote_plus(password)}"
            f"@{host}:{port}/{quote_plus(name)}"
        )

    return "postgresql://postgres:postgres@postgres:5432/codebase_agent"


DATABASE_URL = _database_url_from_env()
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
CREATE TABLE IF NOT EXISTS tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    plan TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS repo_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    repo_url TEXT NOT NULL,
    provider_repo_id TEXT,
    default_branch TEXT,
    installation_id TEXT,
    access_status TEXT NOT NULL DEFAULT 'active',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS repo_connections_tenant_repo_idx
    ON repo_connections (tenant_id, repo_url);

CREATE INDEX IF NOT EXISTS repo_connections_tenant_idx
    ON repo_connections (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS repo_indexes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    repo_connection_id UUID NOT NULL REFERENCES repo_connections(id) ON DELETE CASCADE,
    repo_url TEXT NOT NULL,
    commit_sha TEXT,
    branch TEXT,
    manifest_sha256 TEXT NOT NULL,
    root_merkle_sha256 TEXT,
    file_count INT NOT NULL,
    chunk_count INT NOT NULL,
    line_count INT NOT NULL DEFAULT 0,
    embedding_model TEXT,
    status TEXT NOT NULL DEFAULT 'indexing',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS repo_indexes_connection_manifest_idx
    ON repo_indexes (repo_connection_id, manifest_sha256);

CREATE INDEX IF NOT EXISTS repo_indexes_connection_created_idx
    ON repo_indexes (repo_connection_id, created_at DESC);

CREATE TABLE IF NOT EXISTS repo_latest_indexes (
    repo_connection_id UUID PRIMARY KEY REFERENCES repo_connections(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    repo_index_id UUID NOT NULL REFERENCES repo_indexes(id) ON DELETE CASCADE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS repo_index_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    repo_connection_id UUID NOT NULL REFERENCES repo_connections(id) ON DELETE CASCADE,
    repo_index_id UUID REFERENCES repo_indexes(id) ON DELETE SET NULL,
    requested_by TEXT,
    trigger TEXT NOT NULL DEFAULT 'manual',
    target_ref TEXT,
    target_commit_sha TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    attempt_count INT NOT NULL DEFAULT 0,
    priority INT NOT NULL DEFAULT 100,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_code TEXT,
    error_message TEXT,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS repo_index_jobs_connection_status_idx
    ON repo_index_jobs (repo_connection_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS repo_index_jobs_status_priority_idx
    ON repo_index_jobs (status, priority ASC, created_at ASC);

CREATE TABLE IF NOT EXISTS code_chunks (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID,
    repo_connection_id UUID,
    repo_index_id UUID,
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
    ADD COLUMN IF NOT EXISTS tenant_id UUID;

ALTER TABLE IF EXISTS code_chunks
    ADD COLUMN IF NOT EXISTS repo_connection_id UUID;

ALTER TABLE IF EXISTS code_chunks
    ADD COLUMN IF NOT EXISTS repo_index_id UUID;

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
    tenant_id UUID,
    repo_connection_id UUID,
    repo_index_id UUID,
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

ALTER TABLE IF EXISTS dir_summaries
    ADD COLUMN IF NOT EXISTS tenant_id UUID;

ALTER TABLE IF EXISTS dir_summaries
    ADD COLUMN IF NOT EXISTS repo_connection_id UUID;

ALTER TABLE IF EXISTS dir_summaries
    ADD COLUMN IF NOT EXISTS repo_index_id UUID;

CREATE TABLE IF NOT EXISTS repo_index_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID,
    repo_connection_id UUID,
    repo_index_id UUID,
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

ALTER TABLE IF EXISTS repo_index_runs
    ADD COLUMN IF NOT EXISTS tenant_id UUID;

ALTER TABLE IF EXISTS repo_index_runs
    ADD COLUMN IF NOT EXISTS repo_connection_id UUID;

ALTER TABLE IF EXISTS repo_index_runs
    ADD COLUMN IF NOT EXISTS repo_index_id UUID;

CREATE TABLE IF NOT EXISTS repo_files (
    tenant_id UUID,
    repo_connection_id UUID,
    repo_index_id UUID,
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

ALTER TABLE IF EXISTS repo_files
    ADD COLUMN IF NOT EXISTS tenant_id UUID;

ALTER TABLE IF EXISTS repo_files
    ADD COLUMN IF NOT EXISTS repo_connection_id UUID;

ALTER TABLE IF EXISTS repo_files
    ADD COLUMN IF NOT EXISTS repo_index_id UUID;

CREATE TABLE IF NOT EXISTS repo_text_lines (
    tenant_id UUID,
    repo_connection_id UUID,
    repo_index_id UUID,
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

ALTER TABLE IF EXISTS repo_text_lines
    ADD COLUMN IF NOT EXISTS tenant_id UUID;

ALTER TABLE IF EXISTS repo_text_lines
    ADD COLUMN IF NOT EXISTS repo_connection_id UUID;

ALTER TABLE IF EXISTS repo_text_lines
    ADD COLUMN IF NOT EXISTS repo_index_id UUID;

CREATE TABLE IF NOT EXISTS repo_chunk_manifests (
    tenant_id UUID,
    repo_connection_id UUID,
    repo_index_id UUID,
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

ALTER TABLE IF EXISTS repo_chunk_manifests
    ADD COLUMN IF NOT EXISTS tenant_id UUID;

ALTER TABLE IF EXISTS repo_chunk_manifests
    ADD COLUMN IF NOT EXISTS repo_connection_id UUID;

ALTER TABLE IF EXISTS repo_chunk_manifests
    ADD COLUMN IF NOT EXISTS repo_index_id UUID;

CREATE INDEX IF NOT EXISTS repo_chunk_manifests_file_idx
    ON repo_chunk_manifests (repo_url, file_path);

CREATE INDEX IF NOT EXISTS repo_chunk_manifests_file_sha_idx
    ON repo_chunk_manifests (repo_url, file_sha256);

CREATE INDEX IF NOT EXISTS repo_chunk_manifests_embedding_sha_idx
    ON repo_chunk_manifests (repo_url, embedding_sha256);

CREATE TABLE IF NOT EXISTS repo_embedding_cache (
    tenant_id UUID,
    repo_connection_id UUID,
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

ALTER TABLE IF EXISTS repo_embedding_cache
    ADD COLUMN IF NOT EXISTS tenant_id UUID;

ALTER TABLE IF EXISTS repo_embedding_cache
    ADD COLUMN IF NOT EXISTS repo_connection_id UUID;

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
            await cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
            await cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            try:
                await cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            except psycopg.Error as exc:
                logger.warning("Skipping pg_trgm extension setup: %s", exc)

    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            for sql in SESSION_MIGRATION_SQLS:
                await cur.execute(sql)
            await cur.execute(SCHEMA_SQL)

    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(TRIGRAM_INDEX_SQL)
    except psycopg.Error as exc:
        logger.warning("Skipping repo_text_lines trigram index: %s", exc)


@dataclass(frozen=True)
class RepoIndexContext:
    tenant_id: str
    repo_connection_id: str
    repo_index_id: str


def _normalize_repo_url(repo_url: str) -> str:
    return repo_url.rstrip("/")


def _infer_provider(repo_url: str) -> str:
    parsed = urlparse(repo_url)
    if parsed.netloc.endswith("github.com"):
        return "github"
    return "custom_git"


def _provider_repo_id(repo_url: str) -> str | None:
    parsed = urlparse(repo_url)
    if not parsed.netloc:
        return None
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{parsed.netloc}/{path}" if path else parsed.netloc


def _json_value(value: dict | None) -> str:
    return json.dumps(value or {})


def _iso_or_none(value) -> str | None:
    return value.isoformat() if value is not None else None


async def ensure_default_tenant() -> dict:
    slug = os.environ.get("DEFAULT_TENANT_SLUG", "default")
    name = os.environ.get("DEFAULT_TENANT_NAME", "Default Tenant")
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO tenants (slug, name, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (slug) DO UPDATE SET
                name = EXCLUDED.name,
                updated_at = NOW()
            RETURNING id, slug, name, plan, created_at, updated_at
            """,
            (slug, name),
        )
        row = await cur.fetchone()
    return {
        "id": str(row[0]),
        "slug": row[1],
        "name": row[2],
        "plan": row[3],
        "created_at": row[4].isoformat(),
        "updated_at": row[5].isoformat(),
    }


async def get_repo_connection(repo_connection_id: str) -> dict | None:
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, tenant_id, provider, repo_url, provider_repo_id,
                   default_branch, installation_id, access_status, metadata,
                   created_at, updated_at
            FROM repo_connections
            WHERE id = %s
            """,
            (repo_connection_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "tenant_id": str(row[1]),
        "provider": row[2],
        "repo_url": row[3],
        "provider_repo_id": row[4],
        "default_branch": row[5],
        "installation_id": row[6],
        "access_status": row[7],
        "metadata": row[8] or {},
        "created_at": row[9].isoformat(),
        "updated_at": row[10].isoformat(),
    }


async def ensure_repo_connection(
    repo_url: str,
    *,
    tenant_id: str | None = None,
    provider: str | None = None,
    default_branch: str | None = None,
    installation_id: str | None = None,
    metadata: dict | None = None,
) -> dict:
    repo_url = _normalize_repo_url(repo_url)
    if tenant_id is None:
        tenant = await ensure_default_tenant()
        tenant_id = tenant["id"]
    provider = provider or _infer_provider(repo_url)

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO repo_connections
                (tenant_id, provider, repo_url, provider_repo_id, default_branch,
                 installation_id, metadata, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, NOW())
            ON CONFLICT (tenant_id, repo_url) DO UPDATE SET
                provider         = EXCLUDED.provider,
                provider_repo_id = EXCLUDED.provider_repo_id,
                default_branch   = COALESCE(EXCLUDED.default_branch, repo_connections.default_branch),
                installation_id  = COALESCE(EXCLUDED.installation_id, repo_connections.installation_id),
                access_status    = 'active',
                metadata         = repo_connections.metadata || EXCLUDED.metadata,
                updated_at       = NOW()
            RETURNING id
            """,
            (
                tenant_id,
                provider,
                repo_url,
                _provider_repo_id(repo_url),
                default_branch,
                installation_id,
                _json_value(metadata),
            ),
        )
        connection_id = str((await cur.fetchone())[0])
    connection = await get_repo_connection(connection_id)
    if connection is None:
        raise RuntimeError(f"Repo connection disappeared after upsert: {connection_id}")
    return connection


async def create_repo_index_job(
    *,
    repo_url: str | None = None,
    repo_connection_id: str | None = None,
    tenant_id: str | None = None,
    provider: str | None = None,
    default_branch: str | None = None,
    installation_id: str | None = None,
    requested_by: str | None = None,
    trigger: str = "manual",
    target_ref: str | None = "HEAD",
    target_commit_sha: str | None = None,
    priority: int = 100,
    metadata: dict | None = None,
) -> dict:
    if repo_connection_id:
        connection = await get_repo_connection(repo_connection_id)
        if connection is None:
            raise ValueError(f"Unknown repo_connection_id: {repo_connection_id}")
    elif repo_url:
        connection = await ensure_repo_connection(
            repo_url,
            tenant_id=tenant_id,
            provider=provider,
            default_branch=default_branch,
            installation_id=installation_id,
            metadata=metadata,
        )
        repo_connection_id = connection["id"]
    else:
        raise ValueError("repo_url or repo_connection_id is required")

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO repo_index_jobs
                (tenant_id, repo_connection_id, requested_by, trigger,
                 target_ref, target_commit_sha, priority)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                connection["tenant_id"],
                repo_connection_id,
                requested_by,
                trigger,
                target_ref,
                target_commit_sha,
                priority,
            ),
        )
        job_id = str((await cur.fetchone())[0])
    job = await get_repo_index_job(job_id)
    if job is None:
        raise RuntimeError(f"Repo index job disappeared after insert: {job_id}")
    return job


async def get_repo_index_job(job_id: str) -> dict | None:
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT j.id, j.tenant_id, j.repo_connection_id, j.repo_index_id,
                   c.repo_url, c.provider, c.default_branch, c.installation_id,
                   c.metadata, j.requested_by, j.trigger, j.target_ref,
                   j.target_commit_sha, j.status, j.attempt_count, j.priority,
                   j.started_at, j.completed_at, j.error_code, j.error_message,
                   j.metrics, j.created_at, j.updated_at
            FROM repo_index_jobs j
            JOIN repo_connections c ON c.id = j.repo_connection_id
            WHERE j.id = %s
            """,
            (job_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "tenant_id": str(row[1]),
        "repo_connection_id": str(row[2]),
        "repo_index_id": str(row[3]) if row[3] is not None else None,
        "repo_url": row[4],
        "provider": row[5],
        "default_branch": row[6],
        "installation_id": row[7],
        "repo_connection_metadata": row[8] or {},
        "requested_by": row[9],
        "trigger": row[10],
        "target_ref": row[11],
        "target_commit_sha": row[12],
        "status": row[13],
        "attempt_count": row[14],
        "priority": row[15],
        "started_at": _iso_or_none(row[16]),
        "completed_at": _iso_or_none(row[17]),
        "error_code": row[18],
        "error_message": row[19],
        "metrics": row[20] or {},
        "created_at": row[21].isoformat(),
        "updated_at": row[22].isoformat(),
    }


async def claim_next_repo_index_job() -> dict | None:
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            WITH next_job AS (
                SELECT id
                FROM repo_index_jobs
                WHERE status = 'queued'
                ORDER BY priority ASC, created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE repo_index_jobs
            SET status = 'cloning',
                attempt_count = attempt_count + 1,
                started_at = COALESCE(started_at, NOW()),
                updated_at = NOW()
            WHERE id = (SELECT id FROM next_job)
            RETURNING id
            """
        )
        row = await cur.fetchone()
    return await get_repo_index_job(str(row[0])) if row else None


async def update_repo_index_job_status(
    job_id: str,
    status: str,
    *,
    repo_index_id: str | None = None,
    metrics: dict | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    increment_attempt: bool = False,
) -> dict | None:
    set_sql = ["status = %s", "updated_at = NOW()"]
    params: list[object] = [status]

    if status in {"cloning", "manifesting", "indexing", "embedding", "summarizing"}:
        set_sql.append("started_at = COALESCE(started_at, NOW())")
    if status in {"complete", "failed", "cancelled"}:
        set_sql.append("completed_at = NOW()")
    if status == "complete":
        set_sql.append("error_code = NULL")
        set_sql.append("error_message = NULL")
    if increment_attempt:
        set_sql.append("attempt_count = attempt_count + 1")
    if repo_index_id is not None:
        set_sql.append("repo_index_id = %s")
        params.append(repo_index_id)
    if metrics is not None:
        set_sql.append("metrics = %s::jsonb")
        params.append(_json_value(metrics))
    if error_code is not None:
        set_sql.append("error_code = %s")
        params.append(error_code)
    if error_message is not None:
        set_sql.append("error_message = %s")
        params.append(error_message[:2000])

    params.append(job_id)
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"UPDATE repo_index_jobs SET {', '.join(set_sql)} WHERE id = %s",
            params,
        )
    return await get_repo_index_job(job_id)


async def prepare_repo_index(
    repo_url: str,
    manifest: RepoManifest,
    *,
    text_line_count: int = 0,
    tenant_id: str | None = None,
    repo_connection_id: str | None = None,
    job_id: str | None = None,
    commit_sha: str | None = None,
    branch: str | None = None,
    metadata: dict | None = None,
    status: str = "indexing",
) -> RepoIndexContext:
    repo_url = _normalize_repo_url(repo_url)
    if repo_connection_id:
        connection = await get_repo_connection(repo_connection_id)
        if connection is None:
            raise ValueError(f"Unknown repo_connection_id: {repo_connection_id}")
    else:
        connection = await ensure_repo_connection(repo_url, tenant_id=tenant_id)
        repo_connection_id = connection["id"]

    embedding_model = next(
        (chunk.embedding_model for chunk in manifest.chunks if chunk.embedding_model),
        EMBEDDING_MODEL,
    )

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO repo_indexes
                (tenant_id, repo_connection_id, repo_url, commit_sha, branch,
                 manifest_sha256, root_merkle_sha256, file_count, chunk_count,
                 line_count, embedding_model, status, metadata, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, NOW())
            ON CONFLICT (repo_connection_id, manifest_sha256) DO UPDATE SET
                commit_sha         = COALESCE(EXCLUDED.commit_sha, repo_indexes.commit_sha),
                branch             = COALESCE(EXCLUDED.branch, repo_indexes.branch),
                root_merkle_sha256 = EXCLUDED.root_merkle_sha256,
                file_count         = EXCLUDED.file_count,
                chunk_count        = EXCLUDED.chunk_count,
                line_count         = EXCLUDED.line_count,
                embedding_model    = EXCLUDED.embedding_model,
                status             = EXCLUDED.status,
                metadata           = repo_indexes.metadata || EXCLUDED.metadata,
                updated_at         = NOW()
            RETURNING id
            """,
            (
                connection["tenant_id"],
                repo_connection_id,
                repo_url,
                commit_sha,
                branch,
                manifest.manifest_sha256,
                manifest.manifest_sha256,
                len(manifest.files),
                len(manifest.chunks),
                text_line_count,
                embedding_model,
                status,
                _json_value(metadata),
            ),
        )
        repo_index_id = str((await cur.fetchone())[0])

    if job_id:
        await update_repo_index_job_status(job_id, status, repo_index_id=repo_index_id)

    return RepoIndexContext(
        tenant_id=connection["tenant_id"],
        repo_connection_id=repo_connection_id,
        repo_index_id=repo_index_id,
    )


async def store_chunks(
    repo_url: str,
    chunks: list[CodeChunk],
    *,
    replace: bool = False,
    index_context: RepoIndexContext | None = None,
) -> int:
    """Upsert chunks with their embeddings. Returns number of rows written."""
    rows = [
        (
            index_context.tenant_id if index_context else None,
            index_context.repo_connection_id if index_context else None,
            index_context.repo_index_id if index_context else None,
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
            (tenant_id, repo_connection_id, repo_index_id, repo_url, file_path,
             chunk_sha256, embedding_sha256, embedding_model, chunk_type, name,
             parent_class, start_line, end_line, content, embedding)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (repo_url, chunk_sha256) WHERE chunk_sha256 IS NOT NULL
        DO UPDATE SET
            tenant_id        = EXCLUDED.tenant_id,
            repo_connection_id = EXCLUDED.repo_connection_id,
            repo_index_id    = EXCLUDED.repo_index_id,
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


async def store_cached_chunk_embeddings(
    repo_url: str,
    chunks: list[CodeChunk],
    *,
    index_context: RepoIndexContext | None = None,
) -> int:
    """Persist embeddings in a repo-scoped cache keyed by embedding_sha256."""
    rows = [
        (
            index_context.tenant_id if index_context else None,
            index_context.repo_connection_id if index_context else None,
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
                (tenant_id, repo_connection_id, repo_url, embedding_sha256,
                 embedding_model, token_count, embedding, last_used_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (repo_url, embedding_sha256) DO UPDATE SET
                tenant_id        = EXCLUDED.tenant_id,
                repo_connection_id = EXCLUDED.repo_connection_id,
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


async def store_dir_summaries(
    repo_url: str,
    summaries: list[DirSummary],
    *,
    index_context: RepoIndexContext | None = None,
) -> int:
    """Upsert directory summaries with embeddings. Returns rows written."""
    rows = [
        (
            index_context.tenant_id if index_context else None,
            index_context.repo_connection_id if index_context else None,
            index_context.repo_index_id if index_context else None,
            repo_url,
            s.dir_path,
            s.summary,
            s.file_list,
            s.embedding,
        )
        for s in summaries if s.embedding is not None
    ]
    if not rows:
        return 0

    sql = """
        INSERT INTO dir_summaries
            (tenant_id, repo_connection_id, repo_index_id, repo_url, dir_path,
             summary, file_list, embedding)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT ON CONSTRAINT dir_summaries_unique
        DO UPDATE SET
            tenant_id = EXCLUDED.tenant_id,
            repo_connection_id = EXCLUDED.repo_connection_id,
            repo_index_id = EXCLUDED.repo_index_id,
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
    *,
    index_context: RepoIndexContext | None = None,
) -> dict:
    """Persist the latest content-addressed file/chunk manifest for a repo."""
    repo_url = _normalize_repo_url(repo_url)
    metadata = metadata or {}
    if index_context is None:
        index_context = await prepare_repo_index(
            repo_url,
            manifest,
            text_line_count=int(metadata.get("text_line_count") or 0),
            metadata=metadata,
        )

    file_rows = [
        (
            index_context.tenant_id,
            index_context.repo_connection_id,
            index_context.repo_index_id,
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
            index_context.tenant_id,
            index_context.repo_connection_id,
            index_context.repo_index_id,
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
                (tenant_id, repo_connection_id, repo_index_id, repo_url,
                 manifest_sha256, file_count, chunk_count, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            RETURNING id, created_at
            """,
            (
                index_context.tenant_id,
                index_context.repo_connection_id,
                index_context.repo_index_id,
                repo_url,
                manifest.manifest_sha256,
                len(manifest.files),
                len(manifest.chunks),
                _json_value(metadata),
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
                    (tenant_id, repo_connection_id, repo_index_id, repo_url,
                     file_path, file_sha256, size_bytes, language, extension,
                     is_generated, is_vendor, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (repo_url, file_path) DO UPDATE SET
                    tenant_id = EXCLUDED.tenant_id,
                    repo_connection_id = EXCLUDED.repo_connection_id,
                    repo_index_id = EXCLUDED.repo_index_id,
                    file_sha256 = EXCLUDED.file_sha256,
                    size_bytes = EXCLUDED.size_bytes,
                    language = EXCLUDED.language,
                    extension = EXCLUDED.extension,
                    is_generated = EXCLUDED.is_generated,
                    is_vendor = EXCLUDED.is_vendor,
                    updated_at = NOW()
                """,
                file_rows,
            )

        if chunk_rows:
            await cur.executemany(
                """
                INSERT INTO repo_chunk_manifests
                    (tenant_id, repo_connection_id, repo_index_id, repo_url,
                     chunk_sha256, embedding_sha256, embedding_model, file_path,
                     file_sha256, chunk_type, name, parent_class, start_line,
                     end_line, content_bytes, token_count, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (repo_url, chunk_sha256) DO UPDATE SET
                    tenant_id        = EXCLUDED.tenant_id,
                    repo_connection_id = EXCLUDED.repo_connection_id,
                    repo_index_id    = EXCLUDED.repo_index_id,
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

        await cur.execute(
            """
            UPDATE repo_indexes
            SET status = 'complete',
                metadata = metadata || %s::jsonb,
                updated_at = NOW(),
                completed_at = NOW()
            WHERE id = %s
            """,
            (_json_value(metadata), index_context.repo_index_id),
        )
        await cur.execute(
            """
            INSERT INTO repo_latest_indexes
                (repo_connection_id, tenant_id, repo_index_id, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (repo_connection_id) DO UPDATE SET
                tenant_id = EXCLUDED.tenant_id,
                repo_index_id = EXCLUDED.repo_index_id,
                updated_at = NOW()
            """,
            (
                index_context.repo_connection_id,
                index_context.tenant_id,
                index_context.repo_index_id,
            ),
        )

    return {
        "run_id": str(run_id),
        "repo_index_id": index_context.repo_index_id,
        "repo_connection_id": index_context.repo_connection_id,
        "tenant_id": index_context.tenant_id,
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
    *,
    index_context: RepoIndexContext | None = None,
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
                index_context.tenant_id if index_context else None,
                index_context.repo_connection_id if index_context else None,
                index_context.repo_index_id if index_context else None,
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
                    (tenant_id, repo_connection_id, repo_index_id, repo_url,
                     file_path, file_sha256, language, line_number, line_text,
                     updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                """,
                rows,
            )

        if index_context:
            await cur.execute(
                """
                UPDATE repo_text_lines
                SET tenant_id = %s,
                    repo_connection_id = %s,
                    repo_index_id = %s,
                    updated_at = NOW()
                WHERE repo_url = %s
                  AND file_path = ANY(%s::text[])
                """,
                (
                    index_context.tenant_id,
                    index_context.repo_connection_id,
                    index_context.repo_index_id,
                    repo_url,
                    current_file_paths,
                ),
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
           created_at, updated_at
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
) -> None:
    sql = """
        INSERT INTO app_startup_plans
            (repo_set_hash, repo_urls, plan_markdown, graph, ambiguities,
             orchestration_findings, analysis_status, model, error, updated_at)
        VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, NOW())
        ON CONFLICT (repo_set_hash) DO UPDATE SET
            repo_urls              = EXCLUDED.repo_urls,
            plan_markdown          = EXCLUDED.plan_markdown,
            graph                  = EXCLUDED.graph,
            ambiguities            = EXCLUDED.ambiguities,
            orchestration_findings = EXCLUDED.orchestration_findings,
            analysis_status        = EXCLUDED.analysis_status,
            model                  = EXCLUDED.model,
            error                  = EXCLUDED.error,
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
            ),
        )
