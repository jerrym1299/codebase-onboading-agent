import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.verification import (
    VerificationResult,
    VerificationStatus,
    result_to_status,
    render_verification_markdown,
)

# Schema parses from raw dict — assessment fields only, no steps_run.
parsed = VerificationResult.model_validate({
    "task": "Verify install + smoke test for p-map",
    "result": "PASS",
    "final_summary": "Plan worked as written.",
})
assert parsed.result == "PASS"
assert parsed.findings == ""

# Status mapping
assert result_to_status("PASS", 1) == VerificationStatus.PASSED
assert result_to_status("BLOCKED", 1) == VerificationStatus.BLOCKED
assert result_to_status("FAIL", 1) == VerificationStatus.RUNNING
assert result_to_status("FAIL", 0) == VerificationStatus.FAILED
assert result_to_status("PARTIAL", 0) == VerificationStatus.FAILED

# Renderer without commands: no "Steps run" section.
md = render_verification_markdown(parsed)
assert "## Task" in md and "## Result\nPASS" in md
assert "## Steps run" not in md
assert "## Findings" not in md

# Renderer with commands: each command rendered with its outcome.
commands = [
    {"command": "npm install", "cwd": "/repos/p-map", "exit_code": 0,
     "stdout_tail": "added 626 packages in 4s", "stderr_tail": ""},
    {"command": "npm test", "cwd": "/repos/p-map", "exit_code": 0,
     "stdout_tail": "50 tests passed", "stderr_tail": ""},
]
md2 = render_verification_markdown(parsed, commands=commands)
assert "## Steps run" in md2
assert "`npm install`" in md2 and "added 626 packages" in md2
assert "`npm test`" in md2 and "50 tests passed" in md2

# BLOCKED case with blockers.
blocked = VerificationResult(
    task="Verify hobbesBackend boot",
    result="BLOCKED",
    findings="DATABASE_URL not supplied by the user; cannot synthesize.",
    blockers=[{"kind": "missing_secret", "detail": "DATABASE_URL"}],
)
md3 = render_verification_markdown(blocked, commands=[])
assert "## Blockers" in md3 and "missing_secret" in md3

print("ok")
