from dataclasses import dataclass

from temporalio import activity
from agents import Runner
from agents.exceptions import MaxTurnsExceeded

from agent_defs import router_agent
from services.clone_repo import ensure_repo_dir
from services.walk_repo import collect_file_paths
from services.chunk_and_embed import chunk_file_list
from services.db import store_chunks, store_dir_summaries, get_pool
from services.dir_summaries import generate_dir_summaries


@dataclass
class IndexParams:
    repo_url: str
    repo_dir: str


@dataclass
class AskParams:
    repo_url: str
    repo_dir: str
    query: str


@dataclass
class WorkflowParams:
    repo_url: str
    query: str


@dataclass
class ChatParams:
    session_id: str
    repo_url: str


@dataclass
class SessionStatusParams:
    session_id: str
    status: str


@activity.defn
async def update_session_status_activity(params: SessionStatusParams) -> None:
    """Set sessions.status and bump last_seen_at."""
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE sessions SET status = %s, last_seen_at = NOW() WHERE id = %s",
            (params.status, params.session_id),
        )


@activity.defn
async def clone_repo_activity(repo_url: str) -> str:
    """Clone the repo (or reuse existing). Returns repo_dir path."""
    repo_dir = await ensure_repo_dir(repo_url.rstrip("/"))
    if repo_dir is None:
        raise RuntimeError(f"Failed to clone {repo_url}")
    return repo_dir


@activity.defn
async def index_repo_activity(params: IndexParams) -> int:
    """Chunk and embed the repo into pgvector. Returns chunk count. Skips if already indexed."""
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM code_chunks WHERE repo_url = %s",
            (params.repo_url,),
        )
        count = (await cur.fetchone())[0]
    if count > 0:
        return count

    paths = await collect_file_paths(params.repo_dir)
    chunks = chunk_file_list(paths)
    await store_chunks(params.repo_url, chunks)

    activity.logger.info("Generating per-directory summaries for %s", params.repo_url)
    dir_sums = generate_dir_summaries(paths, params.repo_dir)
    await store_dir_summaries(params.repo_url, dir_sums)
    activity.logger.info("Stored %d directory summaries", len(dir_sums))

    return len(chunks)


@activity.defn
async def ask_agent_activity(params: AskParams) -> dict:
    """Run the router agent with the user's query. Returns the answer dict."""
    try:
        result = await Runner.run(
            router_agent,
            (
                f"Local codebase path (for list_files/read_file/git_log/search_code/find_references): {params.repo_dir}\n"
                f"Indexed repo_url (for search_indexed): {params.repo_url}\n"
                f"Question: {params.query}\n"
                "Answer concisely, referencing specific files, lines, and functions."
            ),
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
