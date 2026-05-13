import asyncio
import base64
import hashlib
import json
import os
from contextlib import asynccontextmanager

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
    build_graph_activity,
    cancel_pending_actions_activity,
    clone_repo_activity,
    consolidate_plan_activity,
    extract_boundaries_activity,
    index_repo_activity,
    resolve_pending_actions_activity,
    update_session_status_activity,
)

from services.chunk_and_embed import AST_PARSERS, chunk_file_list, dump_ast, embed_query
from services.clone_repo import ensure_repo_dir
from services.db import (
    CODE_SEARCH_SQL, close_pool, get_app_startup_plan_row, get_pool,
    get_repo_boundaries_row, get_session_repo_urls, get_startup_plan_row,
    init_schema, insert_session_repos, store_chunks, get_dir_summaries_for_repo,
)
from services.cleanup import delete_app_plan_data, delete_repo_data, delete_session_data
from services.event_bus import subscribe, unsubscribe
from services.pdf_output import write_markdown_pdf
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
            extract_boundaries_activity,
            build_graph_activity,
            consolidate_plan_activity,
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

def _compute_repo_set_hash(repo_urls: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(repo_urls)).encode("utf-8")).hexdigest()


@app.post("/sessions")
async def create_session_endpoint(payload: dict):
    payload = payload or {}
    raw_urls = payload.get("repo_urls")
    if raw_urls is None:
        legacy = payload.get("repo_url", "")
        raw_urls = [legacy] if legacy else []
    if not isinstance(raw_urls, list) or not raw_urls:
        return {"error": "Missing 'repo_urls' (or legacy 'repo_url')."}

    repo_urls = sorted({u.rstrip("/") for u in raw_urls if isinstance(u, str) and u.strip()})
    if not repo_urls:
        return {"error": "Missing 'repo_urls' (or legacy 'repo_url')."}

    repo_set_hash = _compute_repo_set_hash(repo_urls)

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO sessions (status, app_plan_hash) VALUES ('indexing', %s) RETURNING id",
            (repo_set_hash,),
        )
        session_id = str((await cur.fetchone())[0])

    await insert_session_repos(session_id, repo_urls)

    await app.state.temporal_client.start_workflow(
        CodebaseChatWorkflow.run,
        ChatParams(
            session_id=session_id,
            repo_urls=repo_urls,
            repo_set_hash=repo_set_hash,
        ),
        id=f"chat-{session_id}",
        task_queue="onboarding-queue",
    )
    return {"session_id": session_id, "repo_urls": repo_urls}


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


async def _session_app_plan_hash(session_id: str) -> str | None:
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT app_plan_hash FROM sessions WHERE id = %s", (session_id,),
        )
        row = await cur.fetchone()
    return row[0] if row else None


@app.get("/sessions/{session_id}/startup-plan")
async def get_session_startup_plan_endpoint(session_id: str):
    """App-level consolidated startup plan for the session's repo set."""
    repo_urls = await get_session_repo_urls(session_id)
    if not repo_urls:
        return JSONResponse(status_code=404, content={"error": "Session not found."})
    plan_hash = await _session_app_plan_hash(session_id)
    if not plan_hash:
        return JSONResponse(status_code=404, content={"status": "pending"})
    plan_row = await get_app_startup_plan_row(plan_hash)
    if plan_row is None:
        return JSONResponse(status_code=404, content={"status": "pending"})
    return {
        "repo_set_hash": plan_row["repo_set_hash"],
        "repo_urls": plan_row["repo_urls"],
        "plan_markdown": plan_row["plan_markdown"],
        "graph": plan_row["graph"],
        "ambiguities": plan_row["ambiguities"],
        "orchestration_findings": plan_row["orchestration_findings"],
        "analysis_status": plan_row["analysis_status"],
        "model": plan_row["model"],
        "error": plan_row["error"],
        "updated_at": plan_row["updated_at"],
    }


@app.get("/sessions/{session_id}/repos/{repo_url:path}/startup-plan")
async def get_per_repo_startup_plan_endpoint(session_id: str, repo_url: str):
    """Diagnostic: per-repo startup_plans row. URL-encode the repo_url."""
    repo_urls = await get_session_repo_urls(session_id)
    if not repo_urls:
        return JSONResponse(status_code=404, content={"error": "Session not found."})
    target = repo_url.rstrip("/")
    if target not in repo_urls:
        return JSONResponse(
            status_code=404,
            content={"error": f"Repo not part of session.", "repo_urls": repo_urls},
        )
    plan_row = await get_startup_plan_row(target)
    if plan_row is None:
        return JSONResponse(status_code=404, content={"status": "pending"})
    return {
        "repo_url": target,
        "plan": plan_row["plan"],
        "analysis_status": plan_row["analysis_status"],
        "overall_confidence": plan_row["overall_confidence"],
        "model": plan_row["model"],
        "truncations": plan_row["truncations"],
        "error": plan_row["error"],
        "updated_at": plan_row["updated_at"],
    }

@app.get("/sessions/{session_id}/repos/{repo_url:path}/dir-summaries")
async def get_per_repo_dir_summaries_endpoint(session_id: str, repo_url: str):
    """Diagnostic: per-repo dir_summaries rows. URL-encode the repo_url."""
    repo_urls = await get_session_repo_urls(session_id)
    if not repo_urls:
        return JSONResponse(status_code=404, content={"error": "Session not found."})
    target = repo_url.rstrip("/")
    if target not in repo_urls:
        return JSONResponse(
            status_code=404,
            content={"error": "Repo not part of session.", "repo_urls": repo_urls},
        )
    summaries = await get_dir_summaries_for_repo(target)
    if not summaries:
        return JSONResponse(status_code=404, content={"status": "pending"})
    return {
        "repo_url": target,
        "count": len(summaries),
        "summaries": summaries,
    }


@app.get("/sessions/{session_id}/dir-summaries")
async def get_session_dir_summaries_endpoint(session_id: str):
    """All dir_summaries rows across every repo in the session, grouped by repo_url."""
    repo_urls = await get_session_repo_urls(session_id)
    if not repo_urls:
        return JSONResponse(status_code=404, content={"error": "Session not found."})
    summaries_per_repo = await asyncio.gather(
        *[get_dir_summaries_for_repo(url) for url in repo_urls]
    )
    repos = {
        url: {"count": len(s), "summaries": s}
        for url, s in zip(repo_urls, summaries_per_repo)
    }
    return {
        "session_id": session_id,
        "repo_urls": repo_urls,
        "repos": repos,
    }


@app.get("/sessions/{session_id}/repos/{repo_url:path}/boundaries")
async def get_per_repo_boundaries_endpoint(session_id: str, repo_url: str):
    """Diagnostic: per-repo repo_boundaries row. URL-encode the repo_url."""
    repo_urls = await get_session_repo_urls(session_id)
    if not repo_urls:
        return JSONResponse(status_code=404, content={"error": "Session not found."})
    target = repo_url.rstrip("/")
    if target not in repo_urls:
        return JSONResponse(
            status_code=404,
            content={"error": f"Repo not part of session.", "repo_urls": repo_urls},
        )
    row = await get_repo_boundaries_row(target)
    if row is None:
        return JSONResponse(status_code=404, content={"status": "pending"})
    return {
        "repo_url": target,
        "report": row["report"],
        "analysis_status": row["analysis_status"],
        "model": row["model"],
        "error": row["error"],
        "updated_at": row["updated_at"],
    }


@app.post("/sessions/{session_id}/startup-plan/recompute")
async def post_session_startup_recompute_endpoint(session_id: str, payload: dict | None = None):
    reason = ((payload or {}).get("reason") or "").strip()
    repo_urls = await get_session_repo_urls(session_id)
    if not repo_urls:
        return JSONResponse(status_code=404, content={"error": "Session not found."})
    handle = app.state.temporal_client.get_workflow_handle(f"chat-{session_id}")
    await handle.signal("recompute_startup_plan", reason)
    return JSONResponse(status_code=202, content={"status": "recomputing", "session_id": session_id})


@app.post("/sessions/{session_id}/startup-plan/export")
async def post_session_startup_export_endpoint(session_id: str):
    """Thin wrapper: read app_startup_plans.plan_markdown, render to PDF.
    The consolidator already produced the polished, verified markdown."""
    repo_urls = await get_session_repo_urls(session_id)
    if not repo_urls:
        return JSONResponse(status_code=404, content={"error": "Session not found."})
    plan_hash = await _session_app_plan_hash(session_id)
    if not plan_hash:
        return JSONResponse(status_code=404, content={"status": "pending"})
    plan_row = await get_app_startup_plan_row(plan_hash)
    if plan_row is None or not plan_row.get("plan_markdown"):
        return JSONResponse(status_code=404, content={"status": "pending"})

    markdown = plan_row["plan_markdown"]
    pdf_safe = (
        markdown
        .replace("“", '"').replace("”", '"')
        .replace("‘", "'").replace("’", "'")
        .replace("–", "-").replace("—", "-")
        .replace("…", "...")
        .encode("latin-1", errors="replace").decode("latin-1")
    )
    pdf_path = write_markdown_pdf(pdf_safe, f"/tmp/startup_plans/{session_id}.pdf")
    pdf_b64 = base64.b64encode(pdf_path.read_bytes()).decode("ascii")

    return {
        "session_id": session_id,
        "repo_urls": plan_row["repo_urls"],
        "markdown": markdown,
        "pdf_base64": pdf_b64,
    }


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@app.delete("/sessions/{session_id}")
async def delete_session_endpoint(session_id: str, request: Request):
    """Delete a session and (by default) cascade-delete any per-repo and
    app-level data that no other session is using.

    Query params:
      - cascade_orphan_repos: '1'/'0' (default '1') — when last session over
        a repo is deleted, also drop code_chunks/dir_summaries/startup_plans/
        repo_boundaries for that repo.
      - delete_clones: '1'/'0' (default '0') — also rm -rf the local clone
        directory for any orphaned repo. Safe to leave off; clones are
        idempotently reusable.
    """
    params = request.query_params
    cascade = _truthy(params.get("cascade_orphan_repos") or "1")
    delete_clones = _truthy(params.get("delete_clones"))
    report = await delete_session_data(
        session_id,
        temporal_client=app.state.temporal_client,
        cascade_orphan_repos=cascade,
        delete_clones=delete_clones,
    )
    return report.to_dict()


@app.delete("/repos/{repo_url:path}")
async def delete_repo_endpoint(repo_url: str, request: Request):
    """Force-delete per-repo data regardless of session references. Use when
    a partial index needs to be wiped so the next session reindexes cleanly.

    Query params:
      - delete_clone: '1'/'0' (default '0') — also rm -rf the local clone.
    """
    params = request.query_params
    delete_clone = _truthy(params.get("delete_clone"))
    report = await delete_repo_data(repo_url, delete_clone=delete_clone)
    return report.to_dict()


@app.delete("/app-plans/{repo_set_hash}")
async def delete_app_plan_endpoint(repo_set_hash: str):
    """Delete a consolidated app-level plan row by its repo_set_hash."""
    report = await delete_app_plan_data(repo_set_hash)
    return report.to_dict()


