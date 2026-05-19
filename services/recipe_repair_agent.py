"""Agentic sandbox repair loop for Hobbes repo-demo recipe candidates."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any, Literal

from agents import Agent, AgentOutputSchema, ModelSettings, RunItemStreamEvent, Runner
from agents.exceptions import MaxTurnsExceeded
from agents.items import ToolCallItem, ToolCallOutputItem
from pydantic import BaseModel, Field

from services.db import (
    get_pool,
    insert_session_repos,
    list_sandbox_command_runs,
    list_sandbox_runs_for_session,
    update_sandbox_run,
)
from services.github_app import GitHubAppError, GitHubAppService
from services.sandbox_runner import (
    DaytonaSandboxRunner,
    DockerSidecarSandbox,
    LocalSandboxRunner,
    current_sandbox,
)
from services.tools import (
    current_session_id,
    list_files,
    read_file,
    read_background_process_output,
    run_shell,
    search_code,
    start_background_process,
    stop_background_process,
)


RECIPE_REPAIR_AGENT_MODEL = os.environ.get(
    "RECIPE_REPAIR_AGENT_MODEL",
    os.environ.get("RECIPE_REPAIR_MODEL", os.environ.get("RECIPE_CANDIDATE_MODEL", "gpt-5.4")),
)


class RepairBlocker(BaseModel):
    kind: str
    detail: str


class RecipeRepairAgentError(RuntimeError):
    """Raised when the sandbox repair agent cannot safely run."""


class RepairHypothesis(BaseModel):
    id: str = Field(description="Stable short id, e.g. H1.")
    issue: str
    rationale: str
    evidence_to_collect: str
    likely_candidate_change: str = ""


class RepairCommandPlan(BaseModel):
    command: str
    cwd: str
    purpose: str
    success_criteria: str
    failure_implication: str


class RepairStrategy(BaseModel):
    summary: str
    hypotheses: list[RepairHypothesis] = Field(default_factory=list)
    planned_commands: list[RepairCommandPlan] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)


class CandidateDiff(BaseModel):
    field: str
    before: str = ""
    after: str = ""
    reason: str


class RecipeRepairAgentResult(BaseModel):
    """Structured output emitted by the sandbox repair agent."""

    status: Literal["repaired", "blocked", "no_change"]
    revised_candidate: dict[str, Any] | None = Field(
        default=None,
        description="Full Hobbes candidate payload when status is repaired.",
    )
    change_summary: str = ""
    commands_changed: list[str] = Field(default_factory=list)
    confidence: float | None = None
    blockers: list[RepairBlocker] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    candidate_diff: list[CandidateDiff] = Field(default_factory=list)


recipe_repair_strategy_agent = Agent[RepairStrategy](
    name="RecipeRepairStrategyAgent",
    instructions=(
        "You design a bounded repair strategy for a failed Hobbes repo-demo "
        "recipe candidate. Do not repair the candidate yet. Do not run shell "
        "commands. Use file-inspection tools when the supplied observations are "
        "not enough: list files, read manifests/configs, and search code for "
        "ports/env/startup commands. Based on the actual failure bundle and "
        "what you inspect, produce a concise strategy with hypotheses, the "
        "commands the repair agent should run to prove or falsify them, explicit "
        "stop conditions, and risk notes. Prefer the smallest command set that "
        "can decisively justify a candidate change. Never include raw secrets."
    ),
    model=RECIPE_REPAIR_AGENT_MODEL,
    model_settings=ModelSettings(max_tokens=4096),
    output_type=AgentOutputSchema(RepairStrategy, strict_json_schema=False),
    tools=[list_files, read_file, search_code],
)


recipe_sandbox_repair_agent = Agent[RecipeRepairAgentResult](
    name="RecipeSandboxRepairAgent",
    instructions=(
        "You repair failed Hobbes repo-demo recipe candidates by using an isolated "
        "sandbox. You receive the failed candidate, failed verification evidence, "
        "manifest observations, and optionally a deterministic baseline repair.\n\n"
        "Your goal is not to explain the repo. Your goal is to return a safe "
        "structured repair decision:\n"
        "- status='repaired' only when you have enough evidence for a concrete "
        "candidate change.\n"
        "- status='blocked' when credentials, external SaaS, missing databases, "
        "or unclear runtime assumptions prevent safe repair.\n"
        "- status='no_change' when the candidate appears correct and the failure "
        "is environmental or unsupported by evidence.\n\n"
        "Use the tools. Inspect files when needed with list_files/read_file/"
        "search_code. Run commands with run_shell for one-shot checks like "
        "`node --version`, `npm install`, `npm run build`, or `python --version`. "
        "Use start_background_process for dev servers, then read logs and stop "
        "the process before finishing. Do not leave background processes running. "
        "Never run destructive commands. Never include raw secrets in output.\n\n"
        "Hobbes launch plans run dependency setup before service startup. When "
        "validating Node/JavaScript services, run the appropriate install command "
        "(`npm install`, `pnpm install`, or `yarn install`) in the service cwd "
        "before treating missing local binaries such as vite/next/react-scripts "
        "as blockers. Do not prepend dependency installation to the final service "
        "command unless the candidate explicitly lacks any separate setup path; "
        "the revised service command should normally be the steady-state startup "
        "command only.\n\n"
        "Prefer small, decisive trials:\n"
        "1. Inspect package manifests/config files if observations are insufficient.\n"
        "2. Run the minimum setup/check command required to validate the proposed "
        "startup command.\n"
        "3. If validating a dev server, start it in the background, read logs, "
        "optionally curl the local port, then stop it.\n"
        "4. Return a full revised candidate payload when repaired. Preserve known "
        "candidate fields and mutate only command, cwd, port, env placeholders, "
        "readiness timeouts, database setup, demo URL, warnings, evidence, or "
        "confidence when supported by the sandbox evidence.\n\n"
        "Follow the supplied repair_strategy unless tool evidence falsifies it. "
        "If you deviate, mention why in evidence/candidate_diff. Populate "
        "candidate_diff with each changed candidate field, before/after values, "
        "and the concrete evidence that justified the change.\n\n"
        "The final answer must be the structured RecipeRepairAgentResult object."
    ),
    model=RECIPE_REPAIR_AGENT_MODEL,
    model_settings=ModelSettings(max_tokens=16384),
    output_type=AgentOutputSchema(RecipeRepairAgentResult, strict_json_schema=False),
    tools=[
        list_files,
        read_file,
        search_code,
        run_shell,
        start_background_process,
        read_background_process_output,
        stop_background_process,
    ],
)


async def run_sandbox_repair_agent(
    repair_bundle: dict[str, Any],
    *,
    metadata: dict[str, Any],
    observations: dict[str, Any],
    deterministic_repair: dict[str, Any] | None,
) -> dict[str, Any]:
    """Run the sandbox repair agent and return a repair-contract dictionary."""

    repo_url = _repo_url_from_bundle(repair_bundle)
    if not repo_url:
        return _blocked("missing_repo_url", "repair_bundle.repo_context.repo_url is required.")

    session_id = await _create_repair_session(repo_url, metadata=metadata)
    provider = os.environ.get(
        "RECIPE_REPAIR_SANDBOX_PROVIDER",
        "sidecar",
    ).strip().lower()
    repo_auth_tokens = await _repo_auth_tokens_from_bundle(repair_bundle)
    sandbox = _sandbox_for_provider(
        provider,
        session_id=session_id,
        repo_url=repo_url,
        repo_auth_tokens=repo_auth_tokens,
    )

    session_token = current_session_id.set(session_id)
    sandbox_token = current_sandbox.set(sandbox)
    transcript = _RepairTranscriptBuilder(session_id=session_id, provider=provider)
    try:
        strategy = await _generate_repair_strategy(
            session_id=session_id,
            provider=provider,
            repo_url=repo_url,
            repair_bundle=repair_bundle,
            metadata=metadata,
            observations=observations,
            deterministic_repair=deterministic_repair,
        )
        prompt = _build_repair_prompt(
            session_id=session_id,
            provider=provider,
            repo_url=repo_url,
            repair_bundle=repair_bundle,
            metadata=metadata,
            observations=observations,
            deterministic_repair=deterministic_repair,
            strategy=strategy,
        )
        streamed = Runner.run_streamed(
            recipe_sandbox_repair_agent,
            prompt,
            max_turns=_max_turns(),
        )
        async for event in streamed.stream_events():
            transcript.ingest_event(event)

        output = streamed.final_output
        if isinstance(output, RecipeRepairAgentResult):
            response = output.model_dump()
        elif isinstance(output, dict):
            response = output
        else:
            response = _blocked(
                "invalid_agent_output",
                f"Repair agent returned {type(output).__name__}, not a structured result.",
            )
        await transcript.append_persisted_sandbox_runs()
        response.setdefault("model", RECIPE_REPAIR_AGENT_MODEL)
        response.setdefault("usage", {})
        response.setdefault("evidence", [])
        response["repair_strategy"] = strategy.model_dump()
        response["repair_transcript"] = transcript.entries
        response["evidence"] = [
            *[item for item in response.get("evidence", []) if isinstance(item, dict)],
            {
                "source": "sandbox_repair_agent",
                "session_id": session_id,
                "provider": provider,
            },
        ]
        return response
    except RecipeRepairAgentError as exc:
        await transcript.append_persisted_sandbox_runs()
        return {
            **_blocked("strategy_generation_failed", str(exc)[:1000]),
            "model": RECIPE_REPAIR_AGENT_MODEL,
            "usage": {},
            "repair_strategy": None,
            "repair_transcript": transcript.entries,
        }
    except MaxTurnsExceeded:
        await transcript.append_persisted_sandbox_runs()
        return {
            **_blocked(
                "max_turns_exceeded",
                f"Repair agent exceeded max_turns={_max_turns()} before returning a decision.",
            ),
            "model": RECIPE_REPAIR_AGENT_MODEL,
            "usage": {},
            "repair_strategy": None,
            "repair_transcript": transcript.entries,
        }
    finally:
        current_sandbox.reset(sandbox_token)
        current_session_id.reset(session_token)
        await _cleanup_repair_execution(provider, sandbox=sandbox, session_id=session_id)


def _build_repair_prompt(
    *,
    session_id: str,
    provider: str,
    repo_url: str,
    repair_bundle: dict[str, Any],
    metadata: dict[str, Any],
    observations: dict[str, Any],
    deterministic_repair: dict[str, Any] | None,
    strategy: RepairStrategy,
) -> str:
    repo_name = _repo_name(repo_url)
    observed_repo_dir = ""
    if isinstance(observations, dict) and observations.get("clone_status") == "available":
        observed_repo_dir = str(observations.get("repo_dir") or "")
    sandbox_repo_path = f"/repos/{repo_name}"
    shell_cwd = observed_repo_dir if provider == "local" and observed_repo_dir else sandbox_repo_path
    prompt = {
        "task": "Repair a failed Hobbes repo-demo recipe candidate.",
        "session_id": session_id,
        "sandbox_provider": provider,
        "repo": {
            "url": _safe_repo_url(repo_url),
            "file_tool_repo_path": observed_repo_dir or sandbox_repo_path,
            "shell_cwd": shell_cwd,
            "sandbox_repo_path": sandbox_repo_path,
        },
        "metadata": metadata,
        "repair_bundle": repair_bundle,
        "manifest_observations": observations,
        "deterministic_repair_baseline": deterministic_repair,
        "repair_strategy": strategy.model_dump(),
        "constraints": {
            "max_shell_turns": _max_turns(),
            "must_stop_background_processes": True,
            "do_not_emit_raw_secrets": True,
            "return_full_revised_candidate_when_repaired": True,
            "dependency_setup_contract": (
                "Hobbes launch plans run package-manager install steps before "
                "starting services. During sandbox validation, run the relevant "
                "install command before treating missing local package binaries "
                "as terminal blockers. Keep revised service commands focused on "
                "steady-state startup."
            ),
        },
    }
    return json.dumps(prompt, sort_keys=True)


class _RepairTranscriptBuilder:
    def __init__(self, *, session_id: str, provider: str) -> None:
        self.session_id = session_id
        self.provider = provider
        self.entries: list[dict[str, Any]] = []
        self._tool_call_by_id: dict[str, dict[str, Any]] = {}

    def ingest_event(self, event: Any) -> None:
        try:
            if not isinstance(event, RunItemStreamEvent):
                return
            item = event.item
            if isinstance(item, ToolCallItem) and event.name == "tool_called":
                raw = item.raw_item
                call_id = str(getattr(raw, "call_id", "") or getattr(raw, "id", "") or uuid.uuid4())
                name = str(getattr(raw, "name", "") or "")
                args = _parse_tool_args(getattr(raw, "arguments", "") or "{}")
                entry = {
                    "type": "tool_call",
                    "call_id": call_id,
                    "tool_name": name,
                    "args": _redact_value(args),
                }
                self.entries.append(entry)
                self._tool_call_by_id[call_id] = entry
            elif isinstance(item, ToolCallOutputItem) and event.name == "tool_output":
                raw = item.raw_item
                call_id = ""
                if isinstance(raw, dict):
                    call_id = str(raw.get("call_id") or raw.get("id") or "")
                else:
                    call_id = str(getattr(raw, "call_id", "") or getattr(raw, "id", "") or "")
                self.entries.append(
                    {
                        "type": "tool_output",
                        "call_id": call_id,
                        "output_preview": _truncate(_redact_value(item.output), 4000),
                    }
                )
        except Exception:
            self.entries.append({"type": "transcript_error", "detail": "failed to ingest tool event"})

    async def append_persisted_sandbox_runs(self) -> None:
        try:
            runs = await list_sandbox_runs_for_session(self.session_id)
        except Exception:
            return
        for run in runs:
            summary = {
                "type": "sandbox_run",
                "sandbox_run_id": run.get("id"),
                "provider": run.get("provider"),
                "external_id": run.get("external_id"),
                "status": run.get("status"),
                "preview_url": run.get("preview_url"),
                "metadata": _redact_value(run.get("metadata") or {}),
                "commands": [],
            }
            try:
                commands = await list_sandbox_command_runs(run["id"])
            except Exception:
                commands = []
            for command in commands:
                summary["commands"].append(
                    {
                        "command_run_id": command.get("id"),
                        "run_kind": command.get("run_kind"),
                        "command": command.get("command"),
                        "cwd": command.get("cwd"),
                        "status": command.get("status"),
                        "exit_code": command.get("exit_code"),
                        "timed_out": command.get("timed_out"),
                        "duration_ms": command.get("duration_ms"),
                        "stdout_tail": _truncate(_redact_value(command.get("stdout_tail") or ""), 2000),
                        "stderr_tail": _truncate(_redact_value(command.get("stderr_tail") or ""), 2000),
                    }
                )
            self.entries.append(summary)


async def _generate_repair_strategy(
    *,
    session_id: str,
    provider: str,
    repo_url: str,
    repair_bundle: dict[str, Any],
    metadata: dict[str, Any],
    observations: dict[str, Any],
    deterministic_repair: dict[str, Any] | None,
) -> RepairStrategy:
    repo_name = _repo_name(repo_url)
    observed_repo_dir = ""
    if isinstance(observations, dict) and observations.get("clone_status") == "available":
        observed_repo_dir = str(observations.get("repo_dir") or "")
    shell_cwd = observed_repo_dir if provider == "local" and observed_repo_dir else f"/repos/{repo_name}"
    prompt = {
        "task": "Plan a bounded sandbox repair attempt.",
        "session_id": session_id,
        "sandbox_provider": provider,
        "repo_url": _safe_repo_url(repo_url),
        "repo_paths": {
            "file_tool_repo_path": observed_repo_dir or f"/repos/{repo_name}",
            "shell_cwd_for_repair_agent": shell_cwd,
            "sandbox_repo_path": f"/repos/{repo_name}",
        },
        "metadata": metadata,
        "repair_bundle": repair_bundle,
        "manifest_observations": observations,
        "deterministic_repair_baseline": deterministic_repair,
        "constraints": {
            "max_turns": _max_turns(),
            "must_be_decisive": True,
            "do_not_emit_raw_secrets": True,
            "dependency_setup_contract": (
                "For Node/JavaScript services, include package-manager install "
                "as a validation step before startup when node_modules may be "
                "absent or local binaries are missing. Missing dependencies are "
                "only a blocker if install fails or requires unavailable secrets."
            ),
        },
    }
    try:
        result = await Runner.run(
            recipe_repair_strategy_agent,
            json.dumps(prompt, sort_keys=True),
            max_turns=4,
        )
        output = result.final_output
        if isinstance(output, RepairStrategy):
            return output
        if isinstance(output, dict):
            return RepairStrategy.model_validate(output)
    except Exception as exc:
        raise RecipeRepairAgentError(f"repair strategy generation failed: {exc}") from exc
    raise RecipeRepairAgentError("repair strategy generation did not return a strategy.")


def _sandbox_for_provider(
    provider: str,
    *,
    session_id: str,
    repo_url: str,
    repo_auth_tokens: dict[str, str] | None = None,
):
    if provider == "local":
        return LocalSandboxRunner()
    if provider == "daytona":
        return DaytonaSandboxRunner(repo_auth_tokens=repo_auth_tokens)
    if provider == "sidecar":
        return DockerSidecarSandbox(session_id, [repo_url])
    raise ValueError(f"Unknown RECIPE_REPAIR_SANDBOX_PROVIDER={provider!r}")


async def _repo_auth_tokens_from_bundle(repair_bundle: dict[str, Any]) -> dict[str, str]:
    repo_context = repair_bundle.get("repo_context")
    if not isinstance(repo_context, dict):
        return {}

    repo_url = str(repo_context.get("repo_url") or "").strip()
    installation_id = repo_context.get("github_installation_id")
    if not repo_url or not installation_id:
        return {}

    try:
        token = await GitHubAppService().create_installation_access_token(
            installation_id,
            repository_id=repo_context.get("github_repository_id"),
        )
    except GitHubAppError:
        return {}
    return {repo_url: token.token}


async def _create_repair_session(repo_url: str, *, metadata: dict[str, Any]) -> str:
    app_plan_hash = (
        "repo-demo-repair:"
        f"{metadata.get('recipe_id') or metadata.get('candidate_id') or uuid.uuid4()}"
    )
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO sessions (status, app_plan_hash) VALUES ('repairing', %s) RETURNING id",
            (app_plan_hash,),
        )
        row = await cur.fetchone()
    session_id = str(row[0])
    await insert_session_repos(session_id, [repo_url])
    return session_id


async def _cleanup_repair_execution(provider: str, *, sandbox: Any, session_id: str) -> None:
    if os.environ.get("RECIPE_REPAIR_KEEP_SANDBOX", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return

    if provider == "sidecar" and isinstance(sandbox, DockerSidecarSandbox):
        try:
            await sandbox.cleanup()
        except Exception:
            pass
    elif provider == "daytona":
        await _delete_daytona_sandboxes(session_id)

    try:
        pool = await get_pool()
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE sessions SET status = 'ended', last_seen_at = NOW() WHERE id = %s",
                (session_id,),
            )
    except Exception:
        pass


async def _delete_daytona_sandboxes(session_id: str) -> None:
    try:
        runs = await list_sandbox_runs_for_session(session_id)
    except Exception:
        return

    for run in runs:
        sandbox_id = run.get("external_id")
        if not sandbox_id:
            continue
        try:
            await asyncio.to_thread(_delete_daytona_sandbox_sync, sandbox_id)
            await update_sandbox_run(
                run["id"],
                status="cancelled",
                metadata={"repair_agent_deleted": True},
            )
        except Exception as exc:
            await update_sandbox_run(
                run["id"],
                metadata={"repair_agent_delete_error": str(exc)[:1000]},
            )


def _delete_daytona_sandbox_sync(sandbox_id: str) -> None:
    from daytona import Daytona

    Daytona().get(sandbox_id).delete(timeout=120)


def _blocked(kind: str, detail: str) -> dict[str, Any]:
    return {
        "status": "blocked",
        "revised_candidate": None,
        "change_summary": "Sandbox repair agent could not safely produce a revised candidate.",
        "commands_changed": [],
        "confidence": 0.0,
        "blockers": [{"kind": kind, "detail": detail}],
        "evidence": [],
    }


def _repo_url_from_bundle(repair_bundle: dict[str, Any]) -> str:
    repo_context = repair_bundle.get("repo_context")
    if not isinstance(repo_context, dict):
        return ""
    return str(repo_context.get("repo_url") or "").strip()


def _parse_tool_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw[:1000]}
    return parsed if isinstance(parsed, dict) else {}


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_value(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value[:50]]
    if isinstance(value, str):
        redacted = value
        redacted = _redact_secret_like(redacted)
        return redacted
    return value


def _redact_secret_like(value: str) -> str:
    import re

    patterns = [
        r"sk-[A-Za-z0-9_-]{12,}",
        r"github_pat_[A-Za-z0-9_]{12,}",
        r"ghp_[A-Za-z0-9_]{12,}",
        r"x-access-token:[^@\s]+@",
    ]
    redacted = value
    for pattern in patterns:
        redacted = re.sub(pattern, "***", redacted)
    return redacted


def _truncate(value: Any, max_chars: int) -> Any:
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, default=str)
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + f"\n[...truncated {len(value) - max_chars} chars]"


def _repo_name(repo_url: str) -> str:
    return repo_url.rstrip("/").split("/")[-1].removesuffix(".git") or "repo"


def _max_turns() -> int:
    raw = os.environ.get("RECIPE_REPAIR_AGENT_MAX_TURNS", "24")
    try:
        return max(4, min(int(raw), 80))
    except ValueError:
        return 24


def _safe_repo_url(repo_url: str) -> str:
    if "://" not in repo_url:
        return repo_url
    scheme, rest = repo_url.split("://", 1)
    if "@" not in rest:
        return repo_url
    return f"{scheme}://***@{rest.split('@', 1)[1]}"
