import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.sandbox_runner import is_destructive

DENY = [
    "rm -rf /",
    "rm -rf /*",
    "rm -fr /var",
    ":(){ :|:& };:",
    "dd if=/dev/zero of=/dev/sda",
    "mkfs.ext4 /dev/sda1",
    "shutdown -h now",
]
ALLOW = [
    "rm -rf node_modules",
    "rm -rf ./build",
    "npm install",
    "dd if=/dev/zero of=./testfile bs=1M count=1",
]

for c in DENY:
    assert is_destructive(c), f"should deny: {c}"
for c in ALLOW:
    assert not is_destructive(c), f"should allow: {c}"
print("ok")
