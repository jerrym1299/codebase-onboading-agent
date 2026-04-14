import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from temporalio.client import Client
from temporalio.worker import Worker

from activities import onboard_activity
from workflows import OnboardWorkflow
from services.clone_repo import clone_repo
from services.walk_repo import walk_repo, collect_file_paths
from services.chunk_and_embed import chunk_file_list, py_parser, js_parser, ts_parser, tsx_parser
from services.db import init_schema, store_chunks, close_pool

_AST_PARSERS = {
    ".py": py_parser, ".js": js_parser, ".jsx": js_parser,
    ".ts": ts_parser, ".tsx": tsx_parser,
}


def _ast_dump(node, src: bytes, depth: int = 0, max_depth: int = 3) -> list[str]:
    if depth > max_depth:
        return []
    snippet = src[node.start_byte:node.end_byte].decode(errors="replace").split("\n")[0][:60]
    lines = [f"{'  ' * depth}{node.type} [{node.start_point[0]}:{node.end_point[0]}]  {snippet!r}"]
    for child in node.children:
        lines.extend(_ast_dump(child, src, depth + 1, max_depth))
    return lines


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
        workflows=[OnboardWorkflow],
        activities=[onboard_activity],
    )
    try:
        async with worker:
            yield
    finally:
        await close_pool()


app = FastAPI(lifespan=lifespan)


@app.get("/")
def read_root():
    return {"Hello": "world"}


@app.get("/onboard/{name}")
async def onboard(name: str):
    result = await app.state.temporal_client.execute_workflow(
        OnboardWorkflow.run,
        name,
        id=f"onboard-{name}-{uuid.uuid4()}",
        task_queue="onboarding-queue",
    )
    return {"response": result}

@app.get("/walkrepo/{repo_url:path}")
async def walkrepo_endpoint(repo_url: str):
    repo_dir = f"/repos/{uuid.uuid4()}"
    cloned =  await clone_repo(repo_url, repo_dir)
    if(not cloned):
        return {"error": "Failed to clone repository"}
    file_tree = await walk_repo(repo_dir)
    print(file_tree)
    return {"response": file_tree}


@app.get("/chunks/{repo_url:path}")
async def chunks_endpoint(repo_url: str, preview: int = 300):
    """Clone → collect paths → chunk. Returns chunk metadata + preview."""
    repo_dir = f"/repos/{uuid.uuid4()}"
    if not await clone_repo(repo_url, repo_dir):
        return {"error": "Failed to clone repository"}
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
    repo_dir = f"/repos/{uuid.uuid4()}"
    if not await clone_repo(repo_url, repo_dir):
        return {"error": "Failed to clone repository"}
    paths = await collect_file_paths(repo_dir)

    asts = {}
    for path in paths:
        ext = os.path.splitext(path)[1].lower()
        parser_ = _AST_PARSERS.get(ext)
        if parser_ is None:
            continue
        with open(path, "rb") as f:
            src = f.read()
        tree = parser_.parse(src)
        asts[path] = _ast_dump(tree.root_node, src, max_depth=max_depth)
    print(asts)
    return {"file_count": len(asts), "asts": asts}