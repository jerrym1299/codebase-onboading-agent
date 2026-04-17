import json
import os
from dataclasses import dataclass

from temporalio import activity
from agents import (
    Runner, RunConfig, SQLiteSession,
    RawResponsesStreamEvent, RunItemStreamEvent, AgentUpdatedStreamEvent,
)
from agents.items import (
    MessageOutputItem, ToolCallItem, ToolCallOutputItem,
    HandoffCallItem, HandoffOutputItem,
)
from agents.exceptions import MaxTurnsExceeded

from agent_defs import router_agent
from services.clone_repo import ensure_repo_dir
from services.event_bus import publish
from services.walk_repo import collect_file_paths
from services.chunk_and_embed import chunk_file_list
from services.db import store_chunks, store_dir_summaries, get_pool
from services.dir_summaries import generate_dir_summaries

SESSION_DB_PATH = os.environ.get("AGENT_SESSION_DB", "agent_sessions.db")


@dataclass
class IndexParams:
    repo_url: str
    repo_dir: str


@dataclass
class ChatParams:
    session_id: str
    repo_url: str


@dataclass
class AgentTurnParams:
    session_id: str
    content: str


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


async def _append_part(pool, msg_id: str, part: dict):
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE messages SET parts = parts || %s::jsonb WHERE id = %s",
            (json.dumps([part]), msg_id),
        )


@activity.defn
async def agent_turn_activity(params: AgentTurnParams) -> dict:
    """Stream one router_agent turn. Pushes events to event_bus and persists
    completed items to the messages table. SDK session manages history."""
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT repo_url FROM sessions WHERE id = %s",
            (params.session_id,),
        )
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError(f"Session {params.session_id} not found")
    repo_url = row[0]

    repo_dir = await ensure_repo_dir(repo_url)
    if repo_dir is None:
        raise RuntimeError(f"Failed to resolve repo dir for {repo_url}")

    # Persist user message
    user_parts = [{"type": "text", "text": params.content}]
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO messages (session_id, role, parts) VALUES (%s, 'user', %s::jsonb)",
            (params.session_id, json.dumps(user_parts)),
        )

    # Insert placeholder assistant row
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO messages (session_id, role, parts) "
            "VALUES (%s, 'assistant', '[]'::jsonb) RETURNING id",
            (params.session_id,),
        )
        msg_id = str((await cur.fetchone())[0])

    session = SQLiteSession(params.session_id, SESSION_DB_PATH)

    def prepend_repo_context(history, new_input):
        context = {
            "role": "developer",
            "content": (
                f"Local codebase path: {repo_dir}\n"
                f"Indexed repo_url: {repo_url}\n"
                "You are a senior developer and codebase expert. "
                "Be concise but make sure to fully answer the user's question. "
                "Cite file:line where relevant."
            ),
        }
        return [context] + history + new_input

    try:
        result = Runner.run_streamed(
            router_agent,
            params.content,
            session=session,
            run_config=RunConfig(session_input_callback=prepend_repo_context),
            max_turns=20,
        )

        async for event in result.stream_events():
            if isinstance(event, RawResponsesStreamEvent):
                delta = getattr(event.data, "delta", None)
                if isinstance(delta, str) and delta:
                    await publish(params.session_id, {"type": "text-delta", "text": delta})

            elif isinstance(event, RunItemStreamEvent):
                item = event.item

                if isinstance(item, MessageOutputItem) and event.name == "message_output_completed":
                    texts = []
                    for block in getattr(item.raw_item, "content", []):
                        texts.append(getattr(block, "text", ""))
                    part = {"type": "text", "text": "".join(texts)}
                    await publish(params.session_id, part)
                    await _append_part(pool, msg_id, part)

                elif isinstance(item, ToolCallItem) and event.name == "tool_called":
                    raw = item.raw_item
                    part = {
                        "type": "tool-call",
                        "toolCallId": getattr(raw, "call_id", ""),
                        "toolName": getattr(raw, "name", ""),
                        "args": getattr(raw, "arguments", ""),
                    }
                    await publish(params.session_id, part)
                    await _append_part(pool, msg_id, part)

                elif isinstance(item, ToolCallOutputItem) and event.name == "tool_output":
                    raw = item.raw_item
                    call_id = raw.get("call_id", "") if isinstance(raw, dict) else getattr(raw, "call_id", "")
                    part = {
                        "type": "tool-result",
                        "toolCallId": call_id,
                        "output": str(item.output),
                    }
                    await publish(params.session_id, part)
                    await _append_part(pool, msg_id, part)

                elif isinstance(item, (HandoffCallItem, HandoffOutputItem)):
                    target = getattr(item, "target_agent", None)
                    await publish(params.session_id, {
                        "type": "agent-handoff",
                        "agent": getattr(target, "name", str(target)) if target else "",
                    })

            elif isinstance(event, AgentUpdatedStreamEvent):
                await publish(params.session_id, {
                    "type": "agent-updated",
                    "agent": event.new_agent.name,
                })

        text = str(result.final_output)
        final_part = {"type": "text", "text": text}
        await _append_part(pool, msg_id, final_part)
        await publish(params.session_id, final_part)

    except MaxTurnsExceeded:
        text = "Agent exceeded max turns — try a more specific query."
        fallback = {"type": "text", "text": text}
        await _append_part(pool, msg_id, fallback)
        await publish(params.session_id, fallback)

    await publish(params.session_id, {"type": "done"})
    return {"kind": "done", "message_id": msg_id, "parts": [{"type": "text", "text": text}]}
