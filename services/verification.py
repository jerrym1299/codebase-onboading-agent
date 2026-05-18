from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class VerificationStatus(str, Enum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    PASSED = "passed"
    BLOCKED = "blocked"
    FAILED = "failed"


ResultToken = Literal["PASS", "PARTIAL", "BLOCKED", "FAIL"]


class StepRun(BaseModel):
    command: str
    cwd: str | None = None
    exit_code: int
    outcome: str = Field(description="One-line description of what the command proved")


class PlanUpdateRecord(BaseModel):
    change_summary: str = Field(description="One-line summary of what was changed in the plan")


class BackgroundProcessLeft(BaseModel):
    handle: str
    command: str


class Blocker(BaseModel):
    kind: str = Field(
        description="Short category: missing_secret, install_failure, destructive_denied, out_of_scope, environment_conflict, …"
    )
    detail: str


class VerificationResult(BaseModel):
    """Structured output the Verifier agent must emit when it finishes."""
    task: str = Field(description="One sentence restating what was verified")
    result: ResultToken
    steps_run: list[StepRun] = Field(default_factory=list)
    findings: str = Field(
        default="",
        description=(
            "Only populated when result != PASS. For each issue: what failed, "
            "why (with the failing line of output quoted verbatim), and the "
            "concrete fix."
        ),
    )
    plan_updates: list[PlanUpdateRecord] = Field(
        default_factory=list,
        description="One entry per `update_app_startup_plan` call you made.",
    )
    background_left_running: list[BackgroundProcessLeft] = Field(default_factory=list)
    blockers: list[Blocker] = Field(
        default_factory=list,
        description="Required when result == BLOCKED; empty otherwise.",
    )
    final_summary: str = Field(
        default="",
        description="Any plan changes you made, the final result, ambiguities or concerns flagged.",
    )


def render_verification_markdown(result: VerificationResult) -> str:
    """Render a VerificationResult as markdown for chat-time display."""
    parts: list[str] = []
    parts.append(f"## Task\n{result.task}")
    if result.steps_run:
        parts.append("## Steps run")
        for i, s in enumerate(result.steps_run, 1):
            cwd_str = f" (cwd={s.cwd})" if s.cwd else ""
            parts.append(f"{i}. `{s.command}`{cwd_str} → exit {s.exit_code}, {s.outcome}")
    parts.append(f"## Result\n{result.result}")
    if result.findings:
        parts.append(f"## Findings\n{result.findings}")
    if result.blockers:
        parts.append("## Blockers")
        for b in result.blockers:
            parts.append(f"- **{b.kind}**: {b.detail}")
    if result.plan_updates:
        parts.append("## Plan updates applied")
        for pu in result.plan_updates:
            parts.append(f"- {pu.change_summary}")
    if result.background_left_running:
        parts.append("## Background processes still running")
        for p in result.background_left_running:
            parts.append(f"- `{p.handle}` — {p.command}")
    if result.final_summary:
        parts.append(f"## Final summary\n{result.final_summary}")
    return "\n\n".join(parts)


def result_to_status(result: str, iterations_remaining: int) -> VerificationStatus:
    if result == "PASS":
        return VerificationStatus.PASSED
    if result == "BLOCKED":
        return VerificationStatus.BLOCKED
    if iterations_remaining > 0:
        return VerificationStatus.RUNNING
    return VerificationStatus.FAILED


def empty_report() -> dict:
    return {
        "status": VerificationStatus.NOT_STARTED.value,
        "attempts": [],
        "commands": [],
        "probes": [],
        "blockers": [],
        "plan_updates": [],
        "cleanup": {},
        "final_summary": "",
        "updated_at": _now(),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ReportBuilder:
    report: dict = field(default_factory=empty_report)

    def set_status(self, status: VerificationStatus) -> None:
        self.report["status"] = status.value
        self.report["updated_at"] = _now()

    def add_command(self, command: str, cwd: str | None, exit_code: int,
                    duration_ms: int, stdout_tail: str, stderr_tail: str,
                    denied: bool = False) -> None:
        self.report["commands"].append({
            "command": command,
            "cwd": cwd,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "stdout_tail": stdout_tail[-2000:],
            "stderr_tail": stderr_tail[-2000:],
            "denied": denied,
        })
        if denied:
            self.report["blockers"].append(
                {"kind": "destructive_command", "detail": command[:200]}
            )
        self.report["updated_at"] = _now()

    def add_probe(self, target: str, status_code: int | None, passed: bool) -> None:
        self.report["probes"].append(
            {"target": target, "status_code": status_code, "passed": passed}
        )
        self.report["updated_at"] = _now()

    def add_attempt(self, iteration: int, started_at: str, ended_at: str,
                    result: str, summary: str) -> None:
        self.report["attempts"].append({
            "iteration": iteration,
            "started_at": started_at,
            "ended_at": ended_at,
            "result": result,
            "summary": summary,
        })
        self.report["updated_at"] = _now()

    def add_plan_update(self, iteration: int, change_summary: str) -> None:
        self.report["plan_updates"].append(
            {"iteration": iteration, "change_summary": change_summary}
        )
        self.report["updated_at"] = _now()

    def set_cleanup(self, cleanup: dict) -> None:
        self.report["cleanup"] = cleanup
        self.report["updated_at"] = _now()

    def set_final(self, summary: str) -> None:
        self.report["final_summary"] = summary
        self.report["updated_at"] = _now()
