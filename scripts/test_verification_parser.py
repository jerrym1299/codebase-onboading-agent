import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.verification import (
    parse_verifier_result,
    result_to_status,
    VerificationStatus,
)

cases = [
    ("## Result\nPASS\n", "PASS"),
    ("## Result\n PASS — looks good\n", "PASS"),
    ("## Result\nBLOCKED (missing DATABASE_URL)\n", "BLOCKED"),
    ("## Result\nFAIL\n", "FAIL"),
    ("## Result\nPARTIAL\n", "PARTIAL"),
    ("garbage", "FAIL"),
]
for inp, want in cases:
    got = parse_verifier_result(inp)
    assert got == want, (inp, got, want)

assert result_to_status("PASS", 1) == VerificationStatus.PASSED
assert result_to_status("BLOCKED", 1) == VerificationStatus.BLOCKED
assert result_to_status("FAIL", 1) == VerificationStatus.RUNNING
assert result_to_status("FAIL", 0) == VerificationStatus.FAILED
assert result_to_status("PARTIAL", 0) == VerificationStatus.FAILED
print("ok")
