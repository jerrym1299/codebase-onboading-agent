import asyncio
import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from temporalio.client import Client
from temporalio.worker import Worker
from agents import Runner
from agents.exceptions import MaxTurnsExceeded

from services.event_bus import subscribe, unsubscribe

from activities import (
    ChatParams,
    clone_repo_activity,
    index_repo_activity,
    update_session_status_activity,
    agent_turn_activity,
    cancel_pending_actions_activity,
    resolve_pending_actions_activity,
)
from workflows import CodebaseChatWorkflow

def _raw_query_param(request: Request, key: str) -> str:
    """Pull `key=...` from the raw query string with `%` treated as a literal char.
    Only `+` is decoded to space — everything else is kept verbatim."""
    raw = request.scope.get("query_string", b"").decode("utf-8", errors="replace")
    prefix = f"{key}="
    for part in raw.split("&"):
        if part.startswith(prefix):
            return part[len(prefix):].replace("+", " ")
    return ""

from services.clone_repo import ensure_repo_dir
from services.walk_repo import walk_repo, collect_file_paths
from services.chunk_and_embed import chunk_file_list, AST_PARSERS, dump_ast
from services.db import init_schema, store_chunks, close_pool, get_pool
from agent_defs import explorer_agent
from services.chunk_and_embed import embed_query
CLONE_FAILED = {"error": "Failed to clone repository"}


@asynccontextmanager
async def lifespan(app):
    await init_schema()
    client = await Client.connect(
        os.environ.get("TEMPORAL_HOST", "temporal:7233")
    )
    app.state.temporal_client = client
    worker = Worker(
        client,
        task_queue="onboarding-queue",
        workflows=[CodebaseChatWorkflow],
        activities=[
            clone_repo_activity,
            index_repo_activity,
            update_session_status_activity,
            agent_turn_activity,
            cancel_pending_actions_activity,
            resolve_pending_actions_activity,
        ],
    )
    async with worker:
        yield
    await close_pool()


app = FastAPI(lifespan=lifespan)


@app.get("/")
def read_root():
    return {"Hello": "world"}


@app.get("/walkrepo")
async def walkrepo_endpoint(repo_url: str):
    repo_dir = await ensure_repo_dir(repo_url)
    if repo_dir is None:
        return CLONE_FAILED
    return {"response": await walk_repo(repo_dir)}


@app.get("/chunks")
async def chunks_endpoint(repo_url: str, preview: int = 300):
    """Clone → collect paths → chunk → store. Returns chunk metadata + preview."""
    repo_url = repo_url.rstrip("/")
    repo_dir = await ensure_repo_dir(repo_url)
    if repo_dir is None:
        return CLONE_FAILED
    paths = await collect_file_paths(repo_dir)
    chunks = chunk_file_list(paths)
    stored = await store_chunks(repo_url, chunks)
    return {
        "file_count": len(paths),
        "chunk_count": len(chunks),
        "stored": stored,
        "chunks": [
            {
                "index": i,
                "file_path": c.file_path,
                "chunk_type": c.chunk_type,
                "name": c.name,
                "parent_class": c.parent_class,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "token_count": c.token_count,
                "preview": c.embedding_text[:preview],
                "embedding": c.embedding,
            }
            for i, c in enumerate(chunks)
        ],
    }


@app.get("/ast")
async def ast_endpoint(repo_url: str, max_depth: int = 3):
    """Clone → walk → dump tree-sitter AST for every .py/.js/.jsx/.ts/.tsx file."""
    repo_dir = await ensure_repo_dir(repo_url)
    if repo_dir is None:
        return CLONE_FAILED

    asts = {}
    for path in await collect_file_paths(repo_dir):
        parser_ = AST_PARSERS.get(os.path.splitext(path)[1].lower())
        if parser_ is None:
            continue
        with open(path, "rb") as f:
            src = f.read()
        asts[path] = dump_ast(parser_.parse(src).root_node, src, max_depth=max_depth)
    return {"file_count": len(asts), "asts": asts}


@app.get("/explore")
async def explore_endpoint(repo_url: str, request: Request):
    """Explore the codebase with the given query. `query` is read raw — `%` is literal."""
    query = _raw_query_param(request, "query")
    if not query:
        return {"error": "Missing 'query' parameter."}
    repo_dir = await ensure_repo_dir(repo_url)
    if repo_dir is None:
        return CLONE_FAILED
    try:
        result = await Runner.run(
            explorer_agent,
            f"The codebase is at {repo_dir}. {query}",
            max_turns=20,
        )
    except MaxTurnsExceeded:
        return {"error": "Agent exceeded max turns — try a more specific query."}
    return {
        "response": str(result.final_output),
        "last_agent": result.last_agent.name,
        "raw_responses": [
            {
                "role": getattr(item, "type", "unknown"),
                "content": str(item),
            }
            for item in result.raw_responses
        ],
    }

SEARCH_SQL = """
    SELECT file_path, chunk_type, name, start_line, end_line, content,
           1 - (embedding <=> %s::vector) AS similarity
    FROM code_chunks
    WHERE repo_url = %s
    ORDER BY embedding <=> %s::vector
    LIMIT %s
"""


@app.get("/search")
async def search_endpoint(repo_url: str, request: Request, k: int = 10):
    query = _raw_query_param(request, "query")
    if not query:
        return {"error": "Missing 'query' parameter."}
    repo_url = repo_url.rstrip("/")
    emb = "[" + ",".join(repr(x) for x in embed_query(query)) + "]"
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(SEARCH_SQL, (emb, repo_url, emb, k))
        rows = await cur.fetchall()
    return {
        "results": [
        
            {
                "file_path": r[0],
                "chunk_type": r[1],
                "name": r[2],
                "start_line": r[3],
                "end_line": r[4],
                "content": r[5],
                "score": float(r[6]),
            }
            for r in rows
        ],
    }

@app.post("/sessions")
async def create_session_endpoint(payload: dict):
    repo_url = (payload or {}).get("repo_url", "").rstrip("/")
    if not repo_url:
        return {"error": "Missing 'repo_url'."}

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO sessions (repo_url, status) VALUES (%s, 'indexing') RETURNING id",
            (repo_url,),
        )
        session_id = str((await cur.fetchone())[0])

    await app.state.temporal_client.start_workflow(
        CodebaseChatWorkflow.run,
        ChatParams(session_id=session_id, repo_url=repo_url),
        id=f"chat-{session_id}",
        task_queue="onboarding-queue",
    )
    return {"session_id": session_id}


@app.get("/sessions/{session_id}")
async def get_session_endpoint(session_id: str):
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT status FROM sessions WHERE id = %s",
            (session_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return {"error": "Session not found."}
    return {"session_id": session_id, "status": row[0]}


@app.get("/sessions/{session_id}/messages")
async def get_session_messages_endpoint(session_id: str):
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT id, role, parts, created_at FROM messages "
            "WHERE session_id = %s ORDER BY created_at ASC, id ASC",
            (session_id,),
        )
        rows = await cur.fetchall()
    return {
        "session_id": session_id,
        "messages": [
            {
                "id": str(r[0]),
                "role": r[1],
                "parts": r[2],
                "created_at": r[3].isoformat(),
            }
            for r in rows
        ],
    }


@app.post("/sessions/{session_id}/messages")
async def post_session_message_endpoint(session_id: str, payload: dict):
    content = (payload or {}).get("content", "").strip()
    if not content:
        return {"error": "Missing 'content'."}

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT status FROM sessions WHERE id = %s", (session_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return {"error": "Session not found."}

    user_parts = [{"type": "text", "text": content}]
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO messages (session_id, role, parts) VALUES (%s, 'user', %s::jsonb)",
            (session_id, json.dumps(user_parts)),
        )

    queue = subscribe(session_id)

    handle = app.state.temporal_client.get_workflow_handle(f"chat-{session_id}")
    await handle.signal(CodebaseChatWorkflow.user_message, content)

    async def event_generator():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=300)
                except asyncio.TimeoutError:
                    break
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "finish":
                    break
        finally:
            unsubscribe(session_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


