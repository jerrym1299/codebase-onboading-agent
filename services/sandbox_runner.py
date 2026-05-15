"""Execution substrate for verifier commands.

The verifier agent should not know whether commands run in the service
container, a Daytona sandbox, or another execution backend. This module defines
that adapter boundary and keeps the current local/container behavior as the
first implementation.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass

from services.db import (
    create_sandbox_command_run,
    ensure_sandbox_run,
    update_sandbox_command_run,
)


OUTPUT_MAX_LINES_CAP = 5000
BG_LOG_MAX_LINES = 5000


def tail_lines(text: str, max_lines: int) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    kept = lines[-max_lines:]
    dropped = len(lines) - max_lines
    return f"[...truncated {dropped} earlier lines; showing last {max_lines}]\n" + "\n".join(kept)


@dataclass(frozen=True)
class CommandResult:
    sandbox_run_id: str
    command_run_id: str
    command: str
    cwd: str
    exit_code: int | None
    duration_ms: int
    timed_out: bool
    stdout: str
    stderr: str


@dataclass(frozen=True)
class BackgroundStartResult:
    sandbox_run_id: str
    command_run_id: str
    handle: str
    pid: int
    command: str
    cwd: str
    name: str


@dataclass(frozen=True)
class BackgroundOutputResult:
    sandbox_run_id: str
    command_run_id: str
    handle: str
    command: str
    cwd: str
    status: str
    lines: list[str]


@dataclass(frozen=True)
class BackgroundStopResult:
    sandbox_run_id: str
    command_run_id: str
    handle: str
    command: str
    signal_used: str
    exit_code: int | None
    lines: list[str]


class SandboxRunner(ABC):
    provider: str

    @abstractmethod
    async def run_command(
        self,
        *,
        session_id: str,
        command: str,
        cwd: str = "",
        timeout_seconds: int = 30,
        max_output_lines: int = 200,
    ) -> CommandResult:
        raise NotImplementedError

    @abstractmethod
    async def start_background_process(
        self,
        *,
        session_id: str,
        command: str,
        cwd: str = "",
        name: str = "",
    ) -> BackgroundStartResult:
        raise NotImplementedError

    @abstractmethod
    async def read_background_process_output(
        self,
        *,
        session_id: str,
        handle: str,
        tail_lines_count: int = 200,
    ) -> BackgroundOutputResult:
        raise NotImplementedError

    @abstractmethod
    async def stop_background_process(
        self,
        *,
        session_id: str,
        handle: str,
        grace_seconds: int = 5,
    ) -> BackgroundStopResult:
        raise NotImplementedError


@dataclass
class _BackgroundProcess:
    handle: str
    session_id: str
    sandbox_run_id: str
    command_run_id: str
    command: str
    cwd: str
    name: str
    pid: int
    proc: "asyncio.subprocess.Process"
    output: "deque[str]"
    started_at: float
    reader_task: asyncio.Task | None = None
    ended_at: float | None = None
    exit_code: int | None = None


class LocalSandboxRunner(SandboxRunner):
    """Runs commands inside the current service container.

    This preserves today's verifier behavior while giving us the same
    persistence and adapter shape a Daytona runner will use.
    """

    provider = "local"

    def __init__(self) -> None:
        self._background: dict[str, _BackgroundProcess] = {}

    async def _ensure_run(self, session_id: str) -> dict:
        return await ensure_sandbox_run(
            session_id,
            provider=self.provider,
            metadata={"execution_scope": "service_container"},
        )

    async def run_command(
        self,
        *,
        session_id: str,
        command: str,
        cwd: str = "",
        timeout_seconds: int = 30,
        max_output_lines: int = 200,
    ) -> CommandResult:
        sandbox_run = await self._ensure_run(session_id)
        command_run = await create_sandbox_command_run(
            sandbox_run_id=sandbox_run["id"],
            session_id=session_id,
            command=command,
            cwd=cwd,
            run_kind="command",
            metadata={"runner_provider": self.provider},
        )

        start = time.monotonic()
        timed_out = False
        stdout = ""
        stderr = ""
        exit_code: int | None = None

        try:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd or None,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                timed_out = True
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    stdout_b, stderr_b = await proc.communicate()
                except Exception:
                    stdout_b, stderr_b = b"", b""
            exit_code = proc.returncode
            stdout = (stdout_b or b"").decode("utf-8", errors="replace")
            stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        except (OSError, FileNotFoundError) as exc:
            stderr = f"failed to start command: {exc}"
            exit_code = None

        duration_ms = int((time.monotonic() - start) * 1000)
        if timed_out:
            status = "timed_out"
        elif exit_code == 0:
            status = "complete"
        else:
            status = "failed"

        await update_sandbox_command_run(
            command_run["id"],
            status=status,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_ms=duration_ms,
            stdout_tail=tail_lines(stdout, max_output_lines),
            stderr_tail=tail_lines(stderr, max_output_lines),
        )

        return CommandResult(
            sandbox_run_id=sandbox_run["id"],
            command_run_id=command_run["id"],
            command=command,
            cwd=cwd,
            exit_code=exit_code,
            duration_ms=duration_ms,
            timed_out=timed_out,
            stdout=stdout,
            stderr=stderr,
        )

    async def start_background_process(
        self,
        *,
        session_id: str,
        command: str,
        cwd: str = "",
        name: str = "",
    ) -> BackgroundStartResult:
        sandbox_run = await self._ensure_run(session_id)
        handle = str(uuid.uuid4())
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd or None,
        )
        command_run = await create_sandbox_command_run(
            sandbox_run_id=sandbox_run["id"],
            session_id=session_id,
            command=command,
            cwd=cwd,
            run_kind="background_process",
            process_handle=handle,
            pid=proc.pid,
            metadata={"runner_provider": self.provider},
        )
        bg = _BackgroundProcess(
            handle=handle,
            session_id=session_id,
            sandbox_run_id=sandbox_run["id"],
            command_run_id=command_run["id"],
            command=command,
            cwd=cwd,
            name=name or command[:40],
            pid=proc.pid,
            proc=proc,
            output=deque(maxlen=BG_LOG_MAX_LINES),
            started_at=time.monotonic(),
        )
        bg.reader_task = asyncio.create_task(self._drain_background_output(bg))
        self._background[handle] = bg
        return BackgroundStartResult(
            sandbox_run_id=sandbox_run["id"],
            command_run_id=command_run["id"],
            handle=handle,
            pid=proc.pid,
            command=command,
            cwd=cwd,
            name=bg.name,
        )

    async def _drain_background_output(self, bg: _BackgroundProcess) -> None:
        assert bg.proc.stdout is not None
        try:
            while True:
                line = await bg.proc.stdout.readline()
                if not line:
                    break
                bg.output.append(line.decode("utf-8", errors="replace").rstrip("\n"))
        finally:
            rc = await bg.proc.wait()
            bg.exit_code = rc
            bg.ended_at = time.monotonic()
            status = "complete" if rc == 0 else "failed"
            await update_sandbox_command_run(
                bg.command_run_id,
                status=status,
                exit_code=rc,
                duration_ms=int((bg.ended_at - bg.started_at) * 1000),
                stdout_tail="\n".join(list(bg.output)[-BG_LOG_MAX_LINES:]),
                stderr_tail="",
            )

    async def read_background_process_output(
        self,
        *,
        session_id: str,
        handle: str,
        tail_lines_count: int = 200,
    ) -> BackgroundOutputResult:
        bg = self._background.get(handle)
        if bg is None:
            raise ValueError(f"no background process with handle={handle!r}")
        if bg.session_id != session_id:
            raise PermissionError(f"handle {handle!r} does not belong to this session")

        tail_lines_count = max(1, min(int(tail_lines_count), BG_LOG_MAX_LINES))
        lines = list(bg.output)[-tail_lines_count:]

        if bg.exit_code is not None:
            status = f"exited (code={bg.exit_code})"
        elif bg.proc.returncode is None:
            status = f"running (pid={bg.pid})"
        else:
            status = f"exited (code={bg.proc.returncode})"

        await update_sandbox_command_run(
            bg.command_run_id,
            stdout_tail="\n".join(lines),
        )
        return BackgroundOutputResult(
            sandbox_run_id=bg.sandbox_run_id,
            command_run_id=bg.command_run_id,
            handle=handle,
            command=bg.command,
            cwd=bg.cwd,
            status=status,
            lines=lines,
        )

    async def stop_background_process(
        self,
        *,
        session_id: str,
        handle: str,
        grace_seconds: int = 5,
    ) -> BackgroundStopResult:
        bg = self._background.get(handle)
        if bg is None:
            raise ValueError(f"no background process with handle={handle!r}")
        if bg.session_id != session_id:
            raise PermissionError(f"handle {handle!r} does not belong to this session")

        grace_seconds = max(0, min(int(grace_seconds), 60))
        signal_used = "none"

        if bg.proc.returncode is None:
            signal_used = "SIGTERM"
            try:
                bg.proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(bg.proc.wait(), timeout=grace_seconds)
            except asyncio.TimeoutError:
                signal_used = "SIGTERM then SIGKILL"
                try:
                    bg.proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(bg.proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass

        if bg.reader_task is not None:
            try:
                await asyncio.wait_for(bg.reader_task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        tail = list(bg.output)[-20:]
        await update_sandbox_command_run(
            bg.command_run_id,
            status="cancelled" if signal_used != "none" else None,
            exit_code=bg.proc.returncode,
            stdout_tail="\n".join(tail),
            stderr_tail="",
            metadata={"stop_signal": signal_used},
        )
        return BackgroundStopResult(
            sandbox_run_id=bg.sandbox_run_id,
            command_run_id=bg.command_run_id,
            handle=handle,
            command=bg.command,
            signal_used=signal_used,
            exit_code=bg.proc.returncode,
            lines=tail,
        )


class DaytonaSandboxRunner(SandboxRunner):
    provider = "daytona"

    async def run_command(self, **kwargs) -> CommandResult:
        raise NotImplementedError("Daytona sandbox runner is not wired yet.")

    async def start_background_process(self, **kwargs) -> BackgroundStartResult:
        raise NotImplementedError("Daytona sandbox runner is not wired yet.")

    async def read_background_process_output(self, **kwargs) -> BackgroundOutputResult:
        raise NotImplementedError("Daytona sandbox runner is not wired yet.")

    async def stop_background_process(self, **kwargs) -> BackgroundStopResult:
        raise NotImplementedError("Daytona sandbox runner is not wired yet.")


_local_runner = LocalSandboxRunner()
_daytona_runner = DaytonaSandboxRunner()


def get_sandbox_runner() -> SandboxRunner:
    provider = os.environ.get("SANDBOX_RUNNER_PROVIDER", "local").strip().lower()
    if provider == "local":
        return _local_runner
    if provider == "daytona":
        return _daytona_runner
    raise ValueError(f"Unknown SANDBOX_RUNNER_PROVIDER={provider!r}")
