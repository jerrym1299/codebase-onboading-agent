import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.verification import (
    VerificationResult,
    VerificationStatus,
    result_to_status,
    render_verification_markdown,
)

# Schema parses from raw dict
parsed = VerificationResult.model_validate({
    "task": "Verify install + smoke test for p-map",
    "result": "PASS",
    "steps_run": [
        {"command": "npm install", "cwd": "/repos/p-map", "exit_code": 0,
         "outcome": "dependencies installed"},
        {"command": "npm test", "cwd": "/repos/p-map", "exit_code": 0,
         "outcome": "50 tests passed"},
    ],
    "final_summary": "Plan worked as written.",
})
assert parsed.result == "PASS"
assert len(parsed.steps_run) == 2

# Status mapping
assert result_to_status("PASS", 1) == VerificationStatus.PASSED
assert result_to_status("BLOCKED", 1) == VerificationStatus.BLOCKED
assert result_to_status("FAIL", 1) == VerificationStatus.RUNNING
assert result_to_status("FAIL", 0) == VerificationStatus.FAILED
assert result_to_status("PARTIAL", 0) == VerificationStatus.FAILED

# Markdown renderer covers each conditional section
md = render_verification_markdown(parsed)
assert "## Task" in md and "## Steps run" in md and "## Result\nPASS" in md
assert "## Findings" not in md  # absent when result == PASS / no findings

blocked = VerificationResult(
    task="Verify hobbesBackend boot",
    result="BLOCKED",
    findings="DATABASE_URL not supplied by the user; cannot synthesize.",
    blockers=[{"kind": "missing_secret", "detail": "DATABASE_URL"}],
)
md2 = render_verification_markdown(blocked)
assert "## Blockers" in md2 and "missing_secret" in md2

print("ok")
