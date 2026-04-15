import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from temporalio.client import Client
from agents import Runner
from agents.exceptions import MaxTurnsExceeded


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
from services.db import init_schema, store_chunks, close_pool
from agent_defs import explorer_agent

CLONE_FAILED = {"error": "Failed to clone repository"}


@asynccontextmanager
async def lifespan(app):
    await init_schema()
    app.state.temporal_client = await Client.connect(
        os.environ.get("TEMPORAL_HOST", "temporal:7233")
    )
    yield
    await close_pool()


app = FastAPI(lifespan=lifespan)


@app.get("/")
def read_root():
    return {"Hello": "world"}


@app.get("/walkrepo/{repo_url:path}")
async def walkrepo_endpoint(repo_url: str):
    repo_dir = await ensure_repo_dir(repo_url)
    if repo_dir is None:
        return CLONE_FAILED
    return {"response": await walk_repo(repo_dir)}


@app.get("/chunks/{repo_url:path}")
async def chunks_endpoint(repo_url: str, preview: int = 300):
    """Clone → collect paths → chunk → store. Returns chunk metadata + preview."""
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


@app.get("/ast/{repo_url:path}")
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


@app.get("/explore/{repo_url:path}")
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
