import json
import os
from dataclasses import dataclass

from temporalio import activity
from agents import (
    Runner, RunConfig, SQLiteSession,
    RawResponsesStreamEvent, RunItemStreamEvent, AgentUpdatedStreamEvent,
)
from agents.items import (
    ToolCallItem, ToolCallOutputItem,
    HandoffCallItem, HandoffOutputItem,
)
from agents.exceptions import MaxTurnsExceeded

from agent_defs import boundary_extractor_agent, router_agent, consolidator_agent, verifier_agent
from services.boundary_extractor import BoundaryReport, build_developer_prompt
from services.clone_repo import ensure_repo_dir
from services.dependency_graph import build_graph
from services.event_bus import publish
from services.tools import current_session_id
from services.walk_repo import collect_file_paths
from services.chunk_and_embed import chunk_file_list
from services.db import (
    get_app_startup_plan_row, get_pool, get_repo_boundaries_row, get_session_repo_urls,
    get_startup_plan_row, store_chunks, store_dir_summaries, upsert_app_startup_plan,
    upsert_repo_boundaries, upsert_startup_plan, update_app_startup_plan_verification,
)
from services.sandbox_runner import (
    DockerSidecarSandbox, current_sandbox,
    register_sandbox, get_sandbox, unregister_sandbox,
)
from services.verification import (
    ReportBuilder, VerificationStatus,
    parse_verifier_result, result_to_status,
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

@dataclass
class ConsolidateParams:
    session_id: str
    repo_set_hash: str
    repo_urls: list[str]
    repo_dirs: dict[str, str]


@dataclass
class PipelineFailedParams:
    session_id: str
    phase: str
    message: str


@dataclass
class VerifyStartupParams:
    session_id: str
    repo_set_hash: str
    repo_urls: list[str]
    force: bool = False


@dataclass
class ResolvePendingActionParams:
    session_id: str
    pending_id: str
    resolved_value: dict



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
    try:
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
        await publish(params.session_id, {
            "type": "data-repo-progress",
            "repo_url": params.repo_url,
            "stage": "walked",
            "file_count": len(paths),
        })

        chunks = chunk_file_list(paths)
        await publish(params.session_id, {
            "type": "data-repo-progress",
            "repo_url": params.repo_url,
            "stage": "chunked",
            "chunk_count": len(chunks),
        })

        await store_chunks(params.repo_url, chunks)
        await publish(params.session_id, {
            "type": "data-repo-progress",
            "repo_url": params.repo_url,
            "stage": "chunks_stored",
            "chunk_count": len(chunks),
        })

        activity.logger.info("Generating per-directory summaries for %s", params.repo_url)

        async def _publish_dir_progress(done: int, total: int, dir_path: str) -> None:
            await publish(params.session_id, {
                "type": "data-repo-progress",
                "repo_url": params.repo_url,
                "stage": "summarising_dirs",
                "done": done,
                "total": total,
                "dir_path": dir_path,
            })

        dir_sums = await generate_dir_summaries(
            chunks, params.repo_dir, on_progress=_publish_dir_progress,
        )
        await store_dir_summaries(params.repo_url, dir_sums)
        activity.logger.info("Stored %d directory summaries", len(dir_sums))
        await publish(params.session_id, {
            "type": "data-repo-progress",
            "repo_url": params.repo_url,
            "stage": "dir_summaries_stored",
            "summary_count": len(dir_sums),
        })

        await publish(params.session_id, {
            "type": "data-repo-progress",
            "repo_url": params.repo_url,
            "stage": "indexed",
            "chunks": len(chunks),
        })
        return len(chunks)
    except Exception as exc:
        await publish(params.session_id, {
            "type": "data-repo-error",
            "repo_url": params.repo_url,
            "stage": "indexing",
            "message": str(exc)[:500],
            "attempt": activity.info().attempt,
        })
        raise


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
    await publish(params.session_id, {
        "type": "data-repo-progress",
        "repo_url": params.repo_url,
        "stage": "analyzing_startup",
    })

    bundle = build_context(params.repo_dir)
    await publish(params.session_id, {
        "type": "data-repo-progress",
        "repo_url": params.repo_url,
        "stage": "startup_context_built",
        "entries": len(bundle.entries),
        "chars": bundle.total_chars,
    })
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
    await publish(params.session_id, {
        "type": "data-repo-progress",
        "repo_url": params.repo_url,
        "stage": "startup_analyzed",
        "status": status,
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
    try:
        return await _build_graph_inner(params)
    except Exception as exc:
        await publish(params.session_id, {
            "type": "data-pipeline-error",
            "stage": "build_graph",
            "message": str(exc)[:500],
            "attempt": activity.info().attempt,
        })
        raise


async def _build_graph_inner(params: BuildGraphParams) -> dict:
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


CONSOLIDATOR_MODEL = "gpt-5.4"


def _build_consolidator_prompt(
    repo_urls: list[str],
    repo_dirs: dict[str, str],
    plan_row: dict,
) -> str:
    repos_block = "\n".join(
        f"- {url} (local: {repo_dirs.get(url, '?')})" for url in repo_urls
    )
    return (
        "Repos in this app:\n"
        f"{repos_block}\n\n"
        "Dependency graph (nodes + edges + topo_order + cycle_breaks):\n"
        f"{json.dumps(plan_row.get('graph') or {}, indent=2)}\n\n"
        "Matcher-flagged ambiguities:\n"
        f"{json.dumps(plan_row.get('ambiguities') or [], indent=2)}\n\n"
        "Orchestration findings (parsed compose / Procfile):\n"
        f"{json.dumps(plan_row.get('orchestration_findings') or [], indent=2)}\n\n"
        "Produce the final markdown document per your instructions. "
        "Begin output now."
    )


@activity.defn
async def consolidate_plan_activity(params: ConsolidateParams) -> dict:
    """Stream consolidator_agent. Persists final markdown to
    app_startup_plans.plan_markdown."""
    plan_row = await get_app_startup_plan_row(params.repo_set_hash)
    if plan_row is None:
        raise RuntimeError(f"app_startup_plans row missing for {params.repo_set_hash}")

    prompt = _build_consolidator_prompt(params.repo_urls, params.repo_dirs, plan_row)
    current_session_id.set(params.session_id)

    await publish(params.session_id, {
        "type": "data-consolidator-started",
        "repo_set_hash": params.repo_set_hash,
    })

    text_chunks: list[str] = []
    try: #run the consolidator agent with the initial prompt
        result = Runner.run_streamed(consolidator_agent, prompt, max_turns=30)
        async for event in result.stream_events():
            if isinstance(event, RawResponsesStreamEvent):
                delta = getattr(event.data, "delta", None)
                if isinstance(delta, str) and delta:
                    text_chunks.append(delta)
                    await publish(params.session_id, {
                        "type": "text-delta",
                        "textDelta": delta,
                    })
            elif isinstance(event, RunItemStreamEvent):
                item = event.item
                if isinstance(item, ToolCallItem) and event.name == "tool_called":
                    raw = item.raw_item
                    await publish(params.session_id, {
                        "type": "tool-input-available",
                        "toolCallId": getattr(raw, "call_id", ""),
                        "toolName": getattr(raw, "name", ""),
                        "args": getattr(raw, "arguments", ""),
                    })
                elif isinstance(item, ToolCallOutputItem) and event.name == "tool_output":
                    raw = item.raw_item
                    call_id = raw.get("call_id", "") if isinstance(raw, dict) else getattr(raw, "call_id", "")
                    await publish(params.session_id, {
                        "type": "tool-output-available",
                        "toolCallId": call_id,
                        "output": str(item.output),
                    })
        markdown = str(result.final_output) if result.final_output else "".join(text_chunks)
        status, error = "ok", None
    except Exception as exc:
        activity.logger.exception("consolidator failed: %s", exc)
        markdown, status, error = "", "failed", str(exc)[:1000]

    await upsert_app_startup_plan(
        repo_set_hash=params.repo_set_hash,
        repo_urls=params.repo_urls,
        plan_markdown=markdown,
        graph=plan_row["graph"],
        ambiguities=plan_row["ambiguities"],
        orchestration_findings=plan_row["orchestration_findings"],
        analysis_status=status,
        model=CONSOLIDATOR_MODEL,
        error=error,
    )

    fresh = await get_app_startup_plan_row(params.repo_set_hash)
    await publish(params.session_id, {
        "type": "data-app-plan-updated",
        "source": "pipeline",
        "updatedAt": fresh["updated_at"] if fresh else None,
        "repo_set_hash": params.repo_set_hash,
    })

    return {"status": status, "markdown_len": len(markdown)}


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _build_verify_prompt(row: dict, params: "VerifyStartupParams",
                         iteration: int, max_iters: int, remaining: int) -> str:
    return (
        "# Automatic startup verification\n\n"
        f"You are iteration {iteration} of {max_iters}. Time remaining: {remaining}s.\n"
        f"session_id: {params.session_id}\n"
        f"repo_set_hash: {params.repo_set_hash}\n"
        f"repos cloned in sidecar at /repos/<repo_name>:\n"
        + "\n".join(f"  - {u}" for u in params.repo_urls) + "\n\n"
        "Current consolidated app startup plan:\n\n"
        "```markdown\n"
        f"{row.get('plan_markdown','')}\n"
        "```\n"
    )


def _ingest_event(ev, builder) -> None:
    # Best-effort: map streamed tool-call/tool-output events into the verification report.
    try:
        from agents.items import ToolCallItem, ToolCallOutputItem
    except Exception:
        return
    try:
        if isinstance(ev, RunItemStreamEvent):
            item = ev.item
            if isinstance(item, ToolCallItem) and ev.name == "tool_called":
                raw = item.raw_item
                name = getattr(raw, "name", "")
                args_raw = getattr(raw, "arguments", "") or "{}"
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
                except Exception:
                    args = {}
                if name == "run_shell":
                    builder.add_command(
                        command=args.get("command", ""),
                        cwd=args.get("cwd"),
                        exit_code=0,
                        duration_ms=0,
                        stdout_tail="",
                        stderr_tail="",
                    )
                elif name in ("update_app_startup_plan", "update_startup_plan"):
                    builder.add_plan_update(
                        iteration=len(builder.report["attempts"]) + 1,
                        change_summary=args.get("change_summary", ""),
                    )
            elif isinstance(item, ToolCallOutputItem) and ev.name == "tool_output":
                out_raw = item.output
                # `output` may be a string (formatted shell result) — we only update on dicts
                if isinstance(out_raw, dict) and "exit_code" in out_raw and builder.report["commands"]:
                    cmd = builder.report["commands"][-1]
                    cmd["exit_code"] = out_raw.get("exit_code", 0)
                    cmd["duration_ms"] = out_raw.get("duration_ms", 0)
                    cmd["stdout_tail"] = (out_raw.get("stdout_tail") or "")[-2000:]
                    cmd["stderr_tail"] = (out_raw.get("stderr_tail") or "")[-2000:]
                    cmd["denied"] = bool(out_raw.get("denied"))
    except Exception:
        # ingestion is best-effort — never crash the verify loop because of an event we don't understand
        pass


@activity.defn
async def verify_startup_activity(params: VerifyStartupParams) -> dict:
    """Run automatic startup verification in a per-session Docker sidecar.

    Spins up the sidecar, clones repos into it, runs verifier_agent in a
    bounded loop, persists structured report. On terminal status the sidecar
    is REGISTERED (kept alive) for chat-time verifier turns. On crash before
    terminal, the sidecar is torn down."""
    import time

    max_iters = int(os.environ.get("VERIFY_MAX_ITERATIONS", "5"))
    budget_seconds = int(os.environ.get("VERIFY_BUDGET_SECONDS", "1200"))

    row = await get_app_startup_plan_row(params.repo_set_hash)
    if row is None:
        return {"status": "skipped", "reason": "no consolidated plan"}

    if not params.force and row.get("verification_status") in (
        VerificationStatus.PASSED.value, VerificationStatus.BLOCKED.value,
        VerificationStatus.FAILED.value,
    ):
        return {"status": "skipped", "reason": "already verified"}

    builder = ReportBuilder()
    builder.set_status(VerificationStatus.RUNNING)
    await update_app_startup_plan_verification(
        params.repo_set_hash, VerificationStatus.RUNNING.value, builder.report)
    await publish(params.session_id, {
        "type": "data-verification-started", "repo_set_hash": params.repo_set_hash})

    existing = get_sandbox(params.session_id)
    if existing is not None and params.force:
        try:
            await existing.cleanup()
        finally:
            unregister_sandbox(params.session_id)

    sandbox = DockerSidecarSandbox(params.session_id, params.repo_urls)
    sandbox_token = None
    session_token = current_session_id.set(params.session_id)
    deadline = time.monotonic() + budget_seconds
    final_status = VerificationStatus.FAILED
    sandbox_started = False
    reached_terminal = False

    try:
        await sandbox.start()
        sandbox_started = True
        sandbox_token = current_sandbox.set(sandbox)

        for iteration in range(1, max_iters + 1):
            if time.monotonic() >= deadline:
                builder.set_final("budget exhausted before iteration could start")
                final_status = VerificationStatus.FAILED
                reached_terminal = True
                break
            iter_start = _iso_now()
            developer_prompt = _build_verify_prompt(
                row, params, iteration, max_iters,
                remaining=int(deadline - time.monotonic()),
            )
            streamed = Runner.run_streamed(verifier_agent, developer_prompt)
            async for ev in streamed.stream_events():
                _ingest_event(ev, builder)
                await update_app_startup_plan_verification(
                    params.repo_set_hash, VerificationStatus.RUNNING.value, builder.report)
            agent_text = str(streamed.final_output) if streamed.final_output else ""
            result = parse_verifier_result(agent_text)
            iter_end = _iso_now()
            builder.add_attempt(iteration, iter_start, iter_end, result,
                                summary=agent_text[:1000])
            row = await get_app_startup_plan_row(params.repo_set_hash)

            status = result_to_status(result, iterations_remaining=max_iters - iteration)
            if status in (VerificationStatus.PASSED, VerificationStatus.BLOCKED):
                final_status = status
                reached_terminal = True
                break
            if iteration == max_iters:
                final_status = VerificationStatus.FAILED
                reached_terminal = True
                break
    except Exception as exc:
        builder.set_final(f"verifier raised: {type(exc).__name__}: {exc}")
        final_status = VerificationStatus.FAILED
        reached_terminal = False
    finally:
        if sandbox_token is not None:
            current_sandbox.reset(sandbox_token)
        current_session_id.reset(session_token)

        if reached_terminal and sandbox_started:
            register_sandbox(params.session_id, sandbox)
            builder.set_cleanup({"kept_alive": True, "container": sandbox.container_name})
        else:
            if sandbox_started:
                cleanup = await sandbox.cleanup()
            else:
                cleanup = {"kept_alive": False, "container": sandbox.container_name,
                           "sidecar_removed": False, "reason": "never started"}
            builder.set_cleanup(cleanup)

        builder.set_status(final_status)
        if not builder.report["final_summary"]:
            builder.set_final(f"verification {final_status.value}")
        await update_app_startup_plan_verification(
            params.repo_set_hash, final_status.value, builder.report)
        await publish(params.session_id, {
            "type": "data-verification-finished",
            "repo_set_hash": params.repo_set_hash,
            "status": final_status.value,
        })

    return {"status": final_status.value, "sandbox_kept_alive": reached_terminal and sandbox_started}


@dataclass
class KillSandboxParams:
    session_id: str


@activity.defn
async def kill_sandbox_activity(params: KillSandboxParams) -> dict:
    sandbox = get_sandbox(params.session_id)
    if sandbox is None:
        return {"killed": False, "reason": "no sandbox"}
    result = await sandbox.cleanup()
    unregister_sandbox(params.session_id)
    await publish(params.session_id, {
        "type": "data-sandbox-killed",
        "session_id": params.session_id,
        "result": result,
    })
    return {"killed": True, **result}


@activity.defn
async def publish_pipeline_failed_activity(params: PipelineFailedParams) -> None:
    """Workflow-side hook: publish a terminal pipeline-failed SSE event after
    Temporal retries are exhausted. Side-effect-only; does not raise."""
    await publish(params.session_id, {
        "type": "data-pipeline-failed",
        "phase": params.phase,
        "message": params.message[:500],
    })


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


@activity.defn
async def resolve_pending_action_activity(params: ResolvePendingActionParams) -> int:
    """Resolve one pending action with the structured value supplied by a signal."""
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE pending_actions "
            "SET status = 'resolved', resolved_value = %s::jsonb, resolved_at = NOW() "
            "WHERE id = %s AND session_id = %s AND status = 'open'",
            (json.dumps(params.resolved_value), params.pending_id, params.session_id),
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

    repo_dirs: dict[str, str] = {}
    for url in repo_urls:
        rd = await ensure_repo_dir(url)
        if rd is None:
            raise RuntimeError(f"Failed to resolve repo dir for {url}")
        repo_dirs[url] = rd

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

    sandbox = get_sandbox(params.session_id)
    sandbox_token = current_sandbox.set(sandbox) if sandbox is not None else None

    def prepend_repo_context(history, new_input):
        repo_lines = "\n".join(
            f"- {url.rstrip('/').split('/')[-1].removesuffix('.git')}: "
            f"local={repo_dirs[url]}, indexed_url={url}"
            for url in repo_urls
        )
        sandbox_line = ""
        if sandbox is not None:
            sandbox_line = (
                f"A verification sandbox container `{sandbox.container_name}` is "
                f"running for this session. Shell commands from the Verifier agent "
                f"will execute inside it. Repos are at /repos/<repo_name> within the "
                f"sandbox.\n"
            )
        context = {
            "role": "developer",
            "content": (
                f"Repos in this session:\n{repo_lines}\n"
                f"{sandbox_line}"
                "When you call search_indexed/search_dir_summaries, pass the indexed_url "
                "that matches the question.\n"
                "When you call read_file/list_files, use the local path of the relevant repo.\n"
                "If the question is ambiguous about which repo, use ask_user to clarify "
                "before calling any tool.\n"
                f"Session id (for get_app_startup_plan): {params.session_id}\n"
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

                if isinstance(item, ToolCallItem) and event.name == "tool_called":
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
    finally:
        if sandbox_token is not None:
            current_sandbox.reset(sandbox_token)

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
