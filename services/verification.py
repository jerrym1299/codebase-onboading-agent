from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class VerificationStatus(str, Enum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    PASSED = "passed"
    BLOCKED = "blocked"
    FAILED = "failed"


_RESULT_TOKENS = ("PASS", "BLOCKED", "PARTIAL", "FAIL")


def parse_verifier_result(agent_output: str) -> str:
    m = re.search(r"^##\s*Result\s*\n([^\n]+)", agent_output, re.MULTILINE)
    if not m:
        return "FAIL"
    line = m.group(1).strip().upper()
    for token in _RESULT_TOKENS:
        if token in line:
            return token
    return "FAIL"


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
