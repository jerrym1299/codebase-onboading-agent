import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import asdict

from agents import Runner
from agents.exceptions import MaxTurnsExceeded
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from temporalio.client import Client
from temporalio.worker import Worker

from agent_defs import explorer_agent
from activities import (
    ChatParams,
    agent_turn_activity,
    analyze_startup_activity,
    cancel_pending_actions_activity,
    clone_repo_activity,
    index_repo_activity,
    resolve_pending_actions_activity,
    update_session_status_activity,
)
from services.chunk_and_embed import AST_PARSERS, chunk_file_list, dump_ast, embed_query
from services.clone_repo import ensure_repo_dir
from services.db import (
    CODE_SEARCH_SQL, close_pool, create_repo_index_job, ensure_repo_connection,
    get_pool, get_repo_index_job, get_startup_plan_row, init_schema,
    prepare_repo_index, search_repo_text_lines, store_chunks,
    store_repo_manifest, store_repo_text_lines,
)
from services.embedding_cache import hydrate_embeddings
from services.exact_search import build_text_lines
from services.event_bus import subscribe, unsubscribe
from services.repo_manifest import build_repo_manifest
from services.walk_repo import collect_file_paths, walk_repo
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
            analyze_startup_activity,
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


@app.get("/health")
def health_endpoint():
    return {"status": "ok"}


@app.post("/repo-connections")
async def create_repo_connection_endpoint(payload: dict):
    repo_url = (payload or {}).get("repo_url", "").rstrip("/")
    if not repo_url:
        return JSONResponse(status_code=400, content={"error": "Missing 'repo_url'."})
    metadata = (payload or {}).get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        return JSONResponse(status_code=400, content={"error": "'metadata' must be an object."})
    connection = await ensure_repo_connection(
        repo_url,
        tenant_id=(payload or {}).get("tenant_id"),
        provider=(payload or {}).get("provider"),
        default_branch=(payload or {}).get("default_branch"),
        installation_id=(payload or {}).get("installation_id"),
        metadata=metadata,
    )
    return connection


@app.post("/repo-index-jobs")
async def create_repo_index_job_endpoint(payload: dict):
    payload = payload or {}
    try:
        job = await create_repo_index_job(
            repo_url=(payload.get("repo_url") or "").rstrip("/") or None,
            repo_connection_id=payload.get("repo_connection_id"),
            tenant_id=payload.get("tenant_id"),
            requested_by=payload.get("requested_by"),
            trigger=payload.get("trigger") or "manual",
            target_ref=payload.get("target_ref") or "HEAD",
            target_commit_sha=payload.get("target_commit_sha"),
            priority=int(payload.get("priority") or 100),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return JSONResponse(status_code=202, content=job)


@app.get("/repo-index-jobs/{job_id}")
async def get_repo_index_job_endpoint(job_id: str):
    job = await get_repo_index_job(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"error": "Job not found."})
    return job


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
    chunks = chunk_file_list(paths, embed=False)
    manifest = build_repo_manifest(repo_dir, paths, chunks)
    text_lines = build_text_lines(repo_dir, manifest)
    index_context = await prepare_repo_index(
        repo_url,
        manifest,
        text_line_count=len(text_lines),
        metadata={"source": "chunks_endpoint"},
    )
    text_lines_stored = await store_repo_text_lines(
        repo_url,
        text_lines,
        manifest.files,
        index_context=index_context,
    )
    embedding_stats = await hydrate_embeddings(
        repo_url,
        chunks,
        index_context=index_context,
    )
    stored = await store_chunks(
        repo_url,
        chunks,
        replace=True,
        index_context=index_context,
    )
    manifest_record = await store_repo_manifest(
        repo_url,
        manifest,
        metadata={
            "source": "chunks_endpoint",
            "embeddings": embedding_stats,
            "summary_generated": False,
            "text_line_count": len(text_lines),
            "text_lines_stored": text_lines_stored,
        },
        index_context=index_context,
    )
    return {
        "file_count": len(paths),
        "chunk_count": len(chunks),
        "stored": stored,
        "text_line_count": len(text_lines),
        "text_lines_stored": text_lines_stored,
        "manifest": manifest_record,
        "embeddings": embedding_stats,
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


@app.get("/manifest")
async def manifest_endpoint(repo_url: str, persist: bool = True, limit: int = 200):
    """Clone -> collect paths -> chunk without embeddings -> return/persist manifest."""
    repo_url = repo_url.rstrip("/")
    repo_dir = await ensure_repo_dir(repo_url)
    if repo_dir is None:
        return CLONE_FAILED

    paths = await collect_file_paths(repo_dir)
    chunks = chunk_file_list(paths, embed=False)
    manifest = build_repo_manifest(repo_dir, paths, chunks)
    text_lines = build_text_lines(repo_dir, manifest)
    limit = max(0, min(limit, 1000))

    stored = None
    text_lines_stored = None
    if persist:
        index_context = await prepare_repo_index(
            repo_url,
            manifest,
            text_line_count=len(text_lines),
            metadata={
                "source": "manifest_endpoint",
                "embedded": False,
                "summary_generated": False,
            },
        )
        text_lines_stored = await store_repo_text_lines(
            repo_url,
            text_lines,
            manifest.files,
            index_context=index_context,
        )
        stored = await store_repo_manifest(
            repo_url,
            manifest,
            metadata={
                "source": "manifest_endpoint",
                "embedded": False,
                "summary_generated": False,
                "text_line_count": len(text_lines),
                "text_lines_stored": text_lines_stored,
            },
            index_context=index_context,
        )

    return {
        "repo_url": repo_url,
        "manifest_sha256": manifest.manifest_sha256,
        "file_count": len(manifest.files),
        "chunk_count": len(manifest.chunks),
        "text_line_count": len(text_lines),
        "text_lines_stored": text_lines_stored,
        "stored": stored,
        "files": [asdict(f) for f in manifest.files[:limit]],
        "chunks": [asdict(c) for c in manifest.chunks[:limit]],
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

@app.get("/search")
async def search_endpoint(repo_url: str, request: Request, k: int = 10):
    query = _raw_query_param(request, "query")
    if not query:
        return {"error": "Missing 'query' parameter."}
    repo_url = repo_url.rstrip("/")
    emb = "[" + ",".join(repr(x) for x in embed_query(query)) + "]"
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(CODE_SEARCH_SQL, (emb, repo_url, emb, k))
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


@app.get("/search-exact")
async def search_exact_endpoint(
    repo_url: str,
    request: Request,
    limit: int = 50,
    regex: bool = False,
    path: str = "",
    language: str = "",
):
    query = _raw_query_param(request, "query")
    if not query:
        return {"error": "Missing 'query' parameter."}
    repo_url = repo_url.rstrip("/")
    try:
        results = await search_repo_text_lines(
            repo_url,
            query,
            regex=regex,
            path=path,
            language=language,
            limit=limit,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"results": results}

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


@app.get("/sessions/{session_id}/startup-plan")
async def get_session_startup_plan_endpoint(session_id: str):
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT repo_url FROM sessions WHERE id = %s", (session_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return JSONResponse(status_code=404, content={"error": "Session not found."})
    repo_url = row[0]
    plan_row = await get_startup_plan_row(repo_url)
    if plan_row is None:
        return JSONResponse(status_code=404, content={"status": "pending"})
    return {
        "repo_url": repo_url,
        "plan": plan_row["plan"],
        "analysis_status": plan_row["analysis_status"],
        "overall_confidence": plan_row["overall_confidence"],
        "model": plan_row["model"],
        "truncations": plan_row["truncations"],
        "error": plan_row["error"],
        "updated_at": plan_row["updated_at"],
    }


@app.post("/sessions/{session_id}/startup-plan/recompute")
async def post_session_startup_recompute_endpoint(session_id: str, payload: dict | None = None):
    reason = ((payload or {}).get("reason") or "").strip()
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT repo_url FROM sessions WHERE id = %s", (session_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return JSONResponse(status_code=404, content={"error": "Session not found."})
    handle = app.state.temporal_client.get_workflow_handle(f"chat-{session_id}")
    await handle.signal("recompute_startup_plan", reason)
    return JSONResponse(status_code=202, content={"status": "recomputing", "session_id": session_id})
