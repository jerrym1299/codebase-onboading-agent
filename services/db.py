"""
Postgres + pgvector persistence for code chunks and embeddings.

Schema is created on startup via init_schema(). Chunks are upserted keyed on
(repo_url, file_path, start_line, chunk_type, name) so re-indexing a repo
overwrites existing rows instead of duplicating them.
"""

import os
import psycopg
from psycopg_pool import AsyncConnectionPool
from pgvector.psycopg import register_vector_async

from services.chunk_and_embed import CodeChunk

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@postgres:5432/codebase_agent",
)

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
    embedding vector(1536) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT code_chunks_unique UNIQUE NULLS NOT DISTINCT
        (repo_url, file_path, start_line, chunk_type, name)
);

CREATE INDEX IF NOT EXISTS code_chunks_embedding_idx
    ON code_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

CREATE INDEX IF NOT EXISTS code_chunks_repo_idx
    ON code_chunks (repo_url);
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
