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

from agent_defs import boundary_extractor_agent, router_agent
from services.boundary_extractor import BoundaryReport, build_developer_prompt
from services.clone_repo import ensure_repo_dir
from services.dependency_graph import build_graph
from services.event_bus import publish
from services.tools import current_session_id
from services.walk_repo import collect_file_paths
from services.chunk_and_embed import chunk_file_list
from services.db import (
    get_pool, get_repo_boundaries_row, get_session_repo_urls, get_startup_plan_row,
    store_chunks, store_dir_summaries, upsert_app_startup_plan,
    upsert_repo_boundaries, upsert_startup_plan,
)
from services.startup_analysis import (
    ANALYSIS_MODEL, build_context, call_llm,
)
from services.dir_summaries import generate_dir_summaries

SESSION_DB_PATH = os.environ.get("AGENT_SESSION_DB", "agent_sessions.db")


@dataclass
class CloneParams:
    repo_url: str
    session_id: str


@dataclass
class IndexParams:
    repo_url: str
    repo_dir: str
    session_id: str


@dataclass
class ChatParams:
    session_id: str
    repo_urls: list[str]
    repo_set_hash: str


@dataclass
class AgentTurnParams:
    session_id: str
    content: str


@dataclass
class SessionStatusParams:
    session_id: str
    status: str


@dataclass
class AnalyzeStartupParams:
    session_id: str
    repo_url: str
    repo_dir: str
    force: bool = False


@dataclass
class ExtractBoundariesParams:
    session_id: str
    repo_url: str
    repo_dir: str


@dataclass
class BuildGraphParams:
    session_id: str
    repo_set_hash: str
    repo_urls: list[str]
    repo_dirs: dict[str, str]


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
async def clone_repo_activity(params: CloneParams) -> str:
    """Clone the repo (or reuse existing). Returns repo_dir path."""
    repo_url = params.repo_url.rstrip("/")
    await publish(params.session_id, {
        "type": "data-repo-progress",
        "repo_url": repo_url,
        "stage": "cloning",
    })
    repo_dir = await ensure_repo_dir(repo_url)
    if repo_dir is None:
        await publish(params.session_id, {
            "type": "data-repo-progress",
            "repo_url": repo_url,
            "stage": "clone_failed",
        })
        raise RuntimeError(f"Failed to clone {repo_url}")
    await publish(params.session_id, {
        "type": "data-repo-progress",
        "repo_url": repo_url,
        "stage": "cloned",
    })
    return repo_dir


@activity.defn
async def index_repo_activity(params: IndexParams) -> int:
    """Chunk and embed the repo into pgvector. Returns chunk count. Skips if already indexed."""
    await publish(params.session_id, {
        "type": "data-repo-progress",
        "repo_url": params.repo_url,
        "stage": "indexing",
    })
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM code_chunks WHERE repo_url = %s",
            (params.repo_url,),
        )
        count = (await cur.fetchone())[0]
    if count > 0:
        await publish(params.session_id, {
            "type": "data-repo-progress",
            "repo_url": params.repo_url,
            "stage": "indexed",
            "skipped": True,
            "chunks": count,
        })
        return count

    paths = await collect_file_paths(params.repo_dir)
    chunks = chunk_file_list(paths)
    await store_chunks(params.repo_url, chunks)

    activity.logger.info("Generating per-directory summaries for %s", params.repo_url)
    dir_sums = generate_dir_summaries(chunks, params.repo_dir)
    await store_dir_summaries(params.repo_url, dir_sums)
    activity.logger.info("Stored %d directory summaries", len(dir_sums))

    await publish(params.session_id, {
        "type": "data-repo-progress",
        "repo_url": params.repo_url,
        "stage": "indexed",
        "chunks": len(chunks),
    })
    return len(chunks)


@activity.defn
async def analyze_startup_activity(params: AnalyzeStartupParams) -> dict:
    """Build a context bundle, call the LLM, validate the plan, persist it,
    and notify the session bus that the plan is updated. Idempotent on
    repo_url unless force=True."""
    if not params.force:
        existing = await get_startup_plan_row(params.repo_url)
        if existing is not None:
            await publish(params.session_id, {
                "type": "data-startup-plan-updated",
                "updatedAt": existing["updated_at"],
            })
            return {"status": existing["analysis_status"], "skipped": True}

    activity.logger.info(
        "Analyzing startup for %s (force=%s)", params.repo_url, params.force,
    )

    bundle = build_context(params.repo_dir)
    activity.logger.info(
        "Context bundle: entries=%d chars=%d truncations=%s",
        len(bundle.entries), bundle.total_chars, bundle.truncations,
    )

    status: str
    plan: dict
    error: str | None = None
    try:
        result = call_llm(bundle)
        plan = result.plan
        status = "ok"
        activity.logger.info(
            "LLM ok: prompt_tokens=%d completion_tokens=%d",
            result.prompt_tokens, result.completion_tokens,
        )
    except json.JSONDecodeError as exc:
        activity.logger.warning("LLM JSON parse failed; retrying once: %s", exc)
        try:
            result = call_llm(bundle)
            plan = result.plan
            status = "ok"
        except Exception as exc2:
            activity.logger.error("LLM retry failed: %s", exc2)
            plan, status = {}, "failed"
            error = f"json_parse: {exc2}"
    except Exception as exc:
        activity.logger.exception("LLM call failed: %s", exc)
        plan, status = {}, "failed"
        error = str(exc)[:1000]

    await upsert_startup_plan(
        repo_url=params.repo_url,
        plan=plan,
        analysis_status=status,
        overall_confidence=None,
        model=ANALYSIS_MODEL,
        truncations=bundle.truncations,
        error=error,
    )

    fresh = await get_startup_plan_row(params.repo_url)
    await publish(params.session_id, {
        "type": "data-startup-plan-updated",
        "updatedAt": fresh["updated_at"] if fresh else None,
    })

    return {"status": status, "skipped": False}


BOUNDARY_EXTRACTOR_MODEL = "gpt-5.4"


@activity.defn
async def extract_boundaries_activity(params: ExtractBoundariesParams) -> dict:
    """Run boundary_extractor_agent over one repo, persist BoundaryReport to
    repo_boundaries. Reads the per-repo startup_plan as prior context."""
    await publish(params.session_id, {
        "type": "data-repo-progress",
        "repo_url": params.repo_url,
        "stage": "extracting_boundaries",
    })

    plan_row = await get_startup_plan_row(params.repo_url)
    plan = plan_row["plan"] if plan_row else None

    prompt = build_developer_prompt(params.repo_dir, params.repo_url, plan)
    current_session_id.set(params.session_id)

    try:
        result = await Runner.run(boundary_extractor_agent, prompt, max_turns=30)
        report: BoundaryReport = result.final_output
        if report.repo_url != params.repo_url:
            report = report.model_copy(update={"repo_url": params.repo_url})
        await upsert_repo_boundaries(
            repo_url=params.repo_url,
            report=report.model_dump(),
            analysis_status="ok",
            model=BOUNDARY_EXTRACTOR_MODEL,
            error=None,
        )
        status = "ok"
    except Exception as exc:
        activity.logger.exception("boundary extractor failed: %s", exc)
        empty = BoundaryReport(repo_url=params.repo_url).model_dump()
        await upsert_repo_boundaries(
            repo_url=params.repo_url,
            report=empty,
            analysis_status="failed",
            model=BOUNDARY_EXTRACTOR_MODEL,
            error=str(exc)[:1000],
        )
        status = "failed"

    await publish(params.session_id, {
        "type": "data-repo-progress",
        "repo_url": params.repo_url,
        "stage": "boundaries_extracted",
        "status": status,
    })
    return {"status": status}


BUILD_GRAPH_MODEL = "deterministic-matcher-v1"


@activity.defn
async def build_graph_activity(params: BuildGraphParams) -> dict:
    """Cross-repo deterministic matcher. Reads repo_boundaries + startup_plans
    rows for every repo in the session, builds a typed DependencyGraph, and
    persists into app_startup_plans with a placeholder plan_markdown="" until
    the consolidator runs."""
    repos = []
    for repo_url in params.repo_urls:
        boundaries_row = await get_repo_boundaries_row(repo_url)
        plan_row = await get_startup_plan_row(repo_url)
        if boundaries_row is None:
            raise RuntimeError(f"Missing repo_boundaries row for {repo_url}")
        report = BoundaryReport.model_validate(boundaries_row["report"])
        plan = plan_row["plan"] if plan_row else None
        repo_dir = params.repo_dirs.get(repo_url, "")
        repos.append((repo_url, repo_dir, report, plan))

    graph, findings, ambiguities = build_graph(repos)

    await upsert_app_startup_plan(
        repo_set_hash=params.repo_set_hash,
        repo_urls=params.repo_urls,
        plan_markdown="",
        graph=graph.model_dump(),
        ambiguities=[a.model_dump() for a in ambiguities],
        orchestration_findings=[f.model_dump() for f in findings],
        analysis_status="partial",
        model=BUILD_GRAPH_MODEL,
        error=None,
    )

    await publish(params.session_id, {
        "type": "data-graph-built",
        "node_count": len(graph.nodes),
        "edge_count": len(graph.edges),
        "ambiguity_count": len(ambiguities),
    })

    return {
        "node_count": len(graph.nodes),
        "edge_count": len(graph.edges),
        "ambiguity_count": len(ambiguities),
        "cycle_break_count": len(graph.cycle_breaks),
    }


@activity.defn
async def cancel_pending_actions_activity(session_id: str) -> int:
    """Cancel all open pending_actions for a session. Returns count cancelled."""
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE pending_actions SET status = 'cancelled', resolved_at = NOW() "
            "WHERE session_id = %s AND status = 'open'",
            (session_id,),
        )
        return cur.rowcount


@activity.defn
async def resolve_pending_actions_activity(session_id: str) -> int:
    """Resolve all open pending_actions for a session (user replied via normal message)."""
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE pending_actions SET status = 'resolved', resolved_at = NOW() "
            "WHERE session_id = %s AND status = 'open'",
            (session_id,),
        )
        return cur.rowcount


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
    repo_urls = await get_session_repo_urls(params.session_id)
    if not repo_urls:
        raise RuntimeError(f"Session {params.session_id} not found")
    repo_url = repo_urls[0]

    repo_dir = await ensure_repo_dir(repo_url)
    if repo_dir is None:
        raise RuntimeError(f"Failed to resolve repo dir for {repo_url}")

    pool = await get_pool()
    # Insert placeholder assistant row
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO messages (session_id, role, parts) "
            "VALUES (%s, 'assistant', '[]'::jsonb) RETURNING id",
            (params.session_id,),
        )
        msg_id = str((await cur.fetchone())[0])

    current_session_id.set(params.session_id)
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
                    await publish(params.session_id, {
                        "type": "text-delta",
                        "textDelta": delta,
                    })

            elif isinstance(event, RunItemStreamEvent):
                item = event.item

                if isinstance(item, MessageOutputItem) and event.name == "message_output_completed":
                    texts = []
                    for block in getattr(item.raw_item, "content", []):
                        texts.append(getattr(block, "text", ""))
                    part = {"type": "text", "text": "".join(texts)}
                    await _append_part(pool, msg_id, part)

                elif isinstance(item, ToolCallItem) and event.name == "tool_called":
                    raw = item.raw_item
                    part = {
                        "type": "tool-input-available",
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
                        "type": "tool-output-available",
                        "toolCallId": call_id,
                        "output": str(item.output),
                    }
                    await publish(params.session_id, part)
                    await _append_part(pool, msg_id, part)

                elif isinstance(item, (HandoffCallItem, HandoffOutputItem)):
                    target = getattr(item, "target_agent", None)
                    part = {
                        "type": "data-handoff",
                        "agent": getattr(target, "name", str(target)) if target else "",
                    }
                    await publish(params.session_id, part)
                    await _append_part(pool, msg_id, part)

            elif isinstance(event, AgentUpdatedStreamEvent):
                await publish(params.session_id, {
                    "type": "data-handoff",
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

    await publish(params.session_id, {"type": "finish"})

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT id, payload FROM pending_actions "
            "WHERE session_id = %s AND status = 'open' "
            "ORDER BY created_at DESC LIMIT 1",
            (params.session_id,),
        )
        pending = await cur.fetchone()

    if pending:
        return {
            "kind": "paused",
            "message_id": msg_id,
            "pending_id": str(pending[0]),
            "payload": pending[1],
        }
    return {"kind": "done", "message_id": msg_id, "parts": [{"type": "text", "text": text}]}
