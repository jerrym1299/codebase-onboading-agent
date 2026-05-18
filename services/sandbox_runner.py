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


class DockerSidecarSandbox:
    def __init__(self, session_id: str, repo_urls: list[str], image: str | None = None):
        self.session_id = session_id
        self.repo_urls = repo_urls
        self.image = image or os.environ.get("VERIFY_SANDBOX_IMAGE", "hobbes-verify-sidecar:latest")
        self.container_name = f"verify-{session_id}"
        self._background: dict[str, BackgroundHandle] = {}
        self._started = False

    async def _run_host(self, args: list[str], timeout: int = 60) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, "", f"host command timed out after {timeout}s"
        return (
            proc.returncode or 0,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
        )

    async def start(self) -> None:
        if self._started:
            return
        token = os.environ.get("GITHUB_TOKEN", "")
        # idempotent: remove any leftover container from a previous attempt
        await self._run_host(["docker", "rm", "-f", self.container_name], timeout=30)
        rc, out, err = await self._run_host([
            "docker", "run", "-d",
            "--name", self.container_name,
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            "--add-host=host.docker.internal:host-gateway",
            "-e", f"GITHUB_TOKEN={token}",
            self.image, "sleep", "infinity",
        ], timeout=120)
        if rc != 0:
            raise RuntimeError(f"sidecar start failed: {err.strip() or out.strip()}")
        await self._run_host(
            ["docker", "exec", self.container_name,
             "mkdir", "-p", SIDECAR_REPO_ROOT, SIDECAR_BG_DIR],
            timeout=30,
        )
        for repo_url in self.repo_urls:
            repo_name = repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
            clone_url = repo_url
            if token and repo_url.startswith("https://github.com/"):
                clone_url = repo_url.replace("https://", f"https://{token}@", 1)
            rc, _, err = await self._run_host([
                "docker", "exec", self.container_name,
                "git", "clone", "--depth", "1", clone_url,
                f"{SIDECAR_REPO_ROOT}/{repo_name}",
            ], timeout=300)
            if rc != 0:
                raise RuntimeError(f"clone failed for {repo_url}: {err.strip()}")
        self._started = True

    async def cleanup(self) -> dict:
        stopped = 0
        for h in list(self._background.keys()):
            try:
                await self.stop_background(h, grace_seconds=2)
                stopped += 1
            except Exception:
                pass
        rc, _, _ = await self._run_host(
            ["docker", "rm", "-f", self.container_name], timeout=30,
        )
        return {"processes_stopped": stopped, "sidecar_removed": rc == 0}

    async def run_shell(self, command: str, cwd: str | None,
                        timeout_seconds: int, max_output_lines: int) -> ShellResult:
        import time
        if is_destructive(command):
            return ShellResult(-1, "", "denied: destructive command", 0, denied=True)
        args = ["docker", "exec"]
        if cwd:
            args += ["-w", cwd]
        args += [self.container_name, "bash", "-lc", command]
        start = time.monotonic()
        rc, out, err = await self._run_host(args, timeout=min(timeout_seconds, 600))
        dur = int((time.monotonic() - start) * 1000)
        return ShellResult(
            exit_code=rc,
            stdout_tail="\n".join(out.splitlines()[-max_output_lines:]),
            stderr_tail="\n".join(err.splitlines()[-max_output_lines:]),
            duration_ms=dur,
        )

    async def start_background(self, command: str, cwd: str | None,
                               name: str | None) -> BackgroundHandle:
        if is_destructive(command):
            raise RuntimeError("denied: destructive command")
        handle = uuid.uuid4().hex[:12]
        log_path = f"{SIDECAR_BG_DIR}/{handle}.log"
        pid_path = f"{SIDECAR_BG_DIR}/{handle}.pid"
        cwd_clause = f"cd {cwd} && " if cwd else ""
        # write PID after nohup so we can poll liveness via kill -0 from outside the sidecar
        launch = (
            f"{cwd_clause}nohup bash -lc {_shquote(command)} "
            f"> {log_path} 2>&1 & echo $! > {pid_path}"
        )
        rc, _, err = await self._run_host(
            ["docker", "exec", self.container_name, "bash", "-lc", launch],
            timeout=30,
        )
        if rc != 0:
            raise RuntimeError(f"background launch failed: {err.strip()}")
        rc2, pid_out, _ = await self._run_host(
            ["docker", "exec", self.container_name, "cat", pid_path],
            timeout=5,
        )
        pid = int(pid_out.strip() or "0") if rc2 == 0 else 0
        h = BackgroundHandle(handle=handle, name=name, command=command, cwd=cwd, pid=pid)
        self._background[handle] = h
        return h

    async def read_background(self, handle: str, tail_lines: int) -> dict:
        h = self._background.get(handle)
        if h is None:
            return {"error": "unknown handle", "running": False}
        rc, out, _ = await self._run_host(
            ["docker", "exec", self.container_name,
             "tail", "-n", str(tail_lines), f"{SIDECAR_BG_DIR}/{handle}.log"],
            timeout=10,
        )
        alive_rc, _, _ = await self._run_host(
            ["docker", "exec", self.container_name, "kill", "-0", str(h.pid)],
            timeout=5,
        )
        return {
            "handle": handle,
            "pid": h.pid,
            "running": alive_rc == 0,
            "output_tail": out if rc == 0 else "",
        }

    async def stop_background(self, handle: str, grace_seconds: int) -> dict:
        h = self._background.get(handle)
        if h is None:
            return {"error": "unknown handle"}
        await self._run_host(
            ["docker", "exec", self.container_name, "kill", str(h.pid)],
            timeout=5,
        )
        await asyncio.sleep(max(grace_seconds, 1))
        alive_rc, _, _ = await self._run_host(
            ["docker", "exec", self.container_name, "kill", "-0", str(h.pid)],
            timeout=5,
        )
        if alive_rc == 0:
            await self._run_host(
                ["docker", "exec", self.container_name, "kill", "-9", str(h.pid)],
                timeout=5,
            )
        self._background.pop(handle, None)
        return {"handle": handle, "stopped": True}


def _shquote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"
