from __future__ import annotations

import asyncio
import os
import re
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol


SIDECAR_REPO_ROOT = "/repos"
SIDECAR_BG_DIR = "/tmp/bg"

DESTRUCTIVE_PATTERNS = [
    re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+/"),
    re.compile(r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*\s+/"),
    re.compile(r":\(\)\s*\{.*\};:"),
    re.compile(r"\bmkfs\.[a-z0-9]+\b"),
    re.compile(r"\bdd\s+if=.*of=/dev/(sd|nvme|mmcblk)"),
    re.compile(r">\s*/dev/sd[a-z]"),
    re.compile(r"\bshutdown\b|\breboot\b|\bhalt\b|\bpoweroff\b"),
]


@dataclass
class ShellResult:
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    duration_ms: int
    denied: bool = False


@dataclass
class BackgroundHandle:
    handle: str
    name: str | None
    command: str
    cwd: str | None
    pid: int


class SandboxRunner(Protocol):
    async def start(self) -> None: ...
    async def run_shell(self, command: str, cwd: str | None,
                        timeout_seconds: int, max_output_lines: int) -> ShellResult: ...
    async def start_background(self, command: str, cwd: str | None,
                               name: str | None) -> BackgroundHandle: ...
    async def read_background(self, handle: str, tail_lines: int) -> dict: ...
    async def stop_background(self, handle: str, grace_seconds: int) -> dict: ...
    async def cleanup(self) -> dict: ...


current_sandbox: ContextVar[SandboxRunner | None] = ContextVar(
    "current_sandbox", default=None
)


_REGISTRY: dict[str, SandboxRunner] = {}


def register_sandbox(session_id: str, sandbox: SandboxRunner) -> None:
    _REGISTRY[session_id] = sandbox


def get_sandbox(session_id: str) -> SandboxRunner | None:
    return _REGISTRY.get(session_id)


def unregister_sandbox(session_id: str) -> SandboxRunner | None:
    return _REGISTRY.pop(session_id, None)


def is_destructive(command: str) -> bool:
    return any(p.search(command) for p in DESTRUCTIVE_PATTERNS)
