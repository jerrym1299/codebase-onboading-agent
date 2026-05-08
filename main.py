import asyncio
import base64
import json
import os
from contextlib import asynccontextmanager

from agents import Runner
from agents.exceptions import MaxTurnsExceeded
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from temporalio.client import Client
from temporalio.worker import Worker

from agent_defs import bootstrap_agent, explorer_agent
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
    CODE_SEARCH_SQL, close_pool, get_pool, get_startup_plan_row, init_schema, store_chunks,
)
from services.event_bus import subscribe, unsubscribe
from services.pdf_output import write_markdown_pdf
from services.tools import current_session_id
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


_EXPORT_PROMPT = """You are producing a concise, user-facing "how to run this repo" document.

Verify the plan below against the actual files at {repo_dir} (indexed as
{repo_url}) using list_files, read_file, and get_dependencies. Use the
verification only to correct inaccuracies — do NOT include the verification
narrative in the output. Do NOT call recompute_startup_plan. The output response should not include
comparisons to the startup plan, the user doesn't know about this, it should just be a polished, final guide
on how to get the application running. 

Output a single markdown document with EXACTLY these sections, in this order:

# Startup plan: <repo name or short title>

## Env vars (MAKE SURE TO INCLUDE ALL REQUIRED AND OPTIONAL ENV VARS, INCLUDING THOSE REFERENCED IN THE CODE BUT NOT IN .env.example or similar., check for these explicitly)

### Required
- `NAME` — one-line description of what this is for.
- (one bullet per required env var; omit this subsection only if there are none)

### Optional
- `NAME` — one-line description.
- (one bullet per optional env var; omit this subsection only if there are none)

## Steps

1. <imperative title>
   ```
   <command>
   ```
2. <imperative title>
   ```
   <command>
   ```
3. ...
4. ...
These steps should only be how to get the application running, it does not need to include further instructions
on how to use the application (e.g. navigating ui, calling the api, etc). Make sure to include steps for installing requirements (and how to do so).


## Runtime

- Language + version, package manager + version. One bullet each. No commentary.

## Services

- One bullet per required service (e.g. `postgres (image: postgres:16)`). Omit
  the section entirely if no services are needed.

## External tools

- One bullet per required tool (e.g. `Docker`, `Node 20+`). Omit the section
  entirely if none.

## Notes

- Anything you were unsure about, low-confidence items, items missing from the
  repo that the user should provide, or corrections worth flagging.
- Be specific. One bullet per concern. Omit the section if there are no
  concerns worth surfacing.
- add a warning section if you are concerned or uncertain about anything.

STRICT FORMATTING RULES:
- Do NOT include phrases like "Verified command:", "Verified requirement:",
  "Verified service:", or "Correction:" in the output.
- Do NOT include examples of what you verified or how you verified it.
- Do NOT cite `file:line` references in the user-facing sections (Env vars,
  Steps, Runtime, Services, External tools). Citations belong only in Notes
  if they help explain a concern.
- Keep each bullet under ~120 characters where possible.

PLAN (JSON, source of truth — verify and correct, don't paraphrase):
{plan_json}
"""


@app.post("/sessions/{session_id}/startup-plan/export")
async def post_session_startup_export_endpoint(session_id: str):
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

    repo_dir = await ensure_repo_dir(repo_url)
    if repo_dir is None:
        return JSONResponse(status_code=500, content={"error": "Failed to resolve repo dir."})

    prompt = _EXPORT_PROMPT.format(
        repo_dir=repo_dir,
        repo_url=repo_url,
        plan_json=json.dumps(plan_row["plan"], indent=2),
    )

    current_session_id.set(session_id)
    try:
        result = await Runner.run(bootstrap_agent, prompt, max_turns=20)
    except MaxTurnsExceeded:
        return JSONResponse(
            status_code=500,
            content={"error": "Agent exceeded max turns while verifying the plan."},
        )

    markdown = str(result.final_output)
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
        "repo_url": repo_url,
        "markdown": markdown,
        "pdf_base64": pdf_b64,
    }


