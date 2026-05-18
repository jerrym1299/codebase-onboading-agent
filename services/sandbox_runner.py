"""Execution substrate for verifier commands.

The verifier agent should not know whether commands run in the service
container, a Daytona sandbox, or another execution backend. This module defines
that adapter boundary and keeps the current local/container behavior as the
first implementation.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import shlex
import time
import uuid
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from services.db import (
    create_sandbox_command_run,
    ensure_sandbox_run,
    get_sandbox_command_run_by_handle,
    get_sandbox_run,
    get_session_repo_urls,
    update_sandbox_command_run,
    update_sandbox_run,
)


OUTPUT_MAX_LINES_CAP = 5000
BG_LOG_MAX_LINES = 5000
WORKSPACE_DIR = "/workspace"
LOCAL_REPOS_DIR = "/repos"

logger = logging.getLogger(__name__)


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


class DaytonaSandboxError(RuntimeError):
    """Raised when the Daytona runner cannot provision or operate a sandbox."""


def _load_daytona_sdk() -> SimpleNamespace:
    try:
        from daytona import CreateSandboxFromImageParams, Daytona, Image, Resources
    except ImportError as exc:
        raise DaytonaSandboxError(
            "Missing `daytona` dependency. Install requirements with `daytona>=0.166.0` "
            "or set SANDBOX_RUNNER_PROVIDER=local."
        ) from exc
    return SimpleNamespace(
        CreateSandboxFromImageParams=CreateSandboxFromImageParams,
        Daytona=Daytona,
        Image=Image,
        Resources=Resources,
    )


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _safe_repo_url(repo_url: str) -> str:
    if "://" not in repo_url:
        return repo_url
    parsed = urlsplit(repo_url)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))


def _repo_name(repo_url: str) -> str:
    name = repo_url.rstrip("/").split("/")[-1].removesuffix(".git")
    return "".join(char if char.isalnum() or char in {"-", "_", "."} else "-" for char in name) or "repo"


def _response_text(response: Any) -> str:
    result = getattr(response, "result", None)
    if result is None:
        result = getattr(response, "stdout", None)
    if result is None:
        return ""
    return str(result)


def _response_exit_code(response: Any) -> int | None:
    exit_code = getattr(response, "exit_code", None)
    return int(exit_code) if exit_code is not None else None


class DaytonaSandboxRunner(SandboxRunner):
    provider = "daytona"

    def __init__(
        self,
        *,
        sdk_loader: Callable[[], SimpleNamespace] | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._sdk_loader = sdk_loader or _load_daytona_sdk
        self._client_factory = client_factory

    def _sdk(self) -> SimpleNamespace:
        return self._sdk_loader()

    def _client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        return self._sdk().Daytona()

    async def _ensure_run(self, session_id: str) -> dict:
        sandbox_run = await ensure_sandbox_run(
            session_id,
            provider=self.provider,
            metadata={"execution_scope": "daytona"},
        )
        if sandbox_run.get("external_id"):
            return sandbox_run

        repo_urls = await get_session_repo_urls(session_id)
        try:
            created = await asyncio.to_thread(
                self._create_remote_sandbox_sync,
                session_id=session_id,
                sandbox_run_id=sandbox_run["id"],
                repo_urls=repo_urls,
            )
        except Exception as exc:
            await update_sandbox_run(
                sandbox_run["id"],
                status="failed",
                metadata={"provision_error": str(exc)[:1000]},
            )
            raise

        updated = await update_sandbox_run(
            sandbox_run["id"],
            external_id=created["external_id"],
            preview_url=created.get("preview_url"),
            metadata=created["metadata"],
        )
        return updated or sandbox_run

    def _create_remote_sandbox_sync(
        self,
        *,
        session_id: str,
        sandbox_run_id: str,
        repo_urls: list[str],
    ) -> dict:
        sdk = self._sdk()
        client = self._client()
        sandbox_name = f"hobbes-agent-{session_id[:8]}-{uuid.uuid4().hex[:8]}"
        image = self._build_image(sdk.Image)
        resources = self._build_resources(sdk)
        labels = {
            "hobbes_kind": "codebase_agent",
            "hobbes_session_id": session_id,
            "hobbes_sandbox_run_id": sandbox_run_id,
        }
        params = sdk.CreateSandboxFromImageParams(
            name=sandbox_name,
            language=os.environ.get("DAYTONA_SANDBOX_LANGUAGE", "javascript"),
            image=image,
            resources=resources,
            labels=labels,
            public=_env_flag("DAYTONA_SANDBOX_PUBLIC", default=True),
            auto_stop_interval=int(os.environ.get("DAYTONA_SANDBOX_AUTO_STOP_MINUTES", "60")),
            auto_archive_interval=int(os.environ.get("DAYTONA_SANDBOX_AUTO_ARCHIVE_MINUTES", "1440")),
            auto_delete_interval=int(os.environ.get("DAYTONA_SANDBOX_AUTO_DELETE_MINUTES", "-1")),
        )
        create_timeout = int(os.environ.get("DAYTONA_SANDBOX_CREATE_TIMEOUT_SECONDS", "300"))
        sandbox = client.create(params, timeout=create_timeout)
        self._ensure_started(sandbox)

        repo_path_map: dict[str, str] = {}
        for repo_url in repo_urls:
            name = _repo_name(repo_url)
            remote_path = f"{WORKSPACE_DIR}/{name}"
            self._clone_repository(sandbox, repo_url, remote_path)
            repo_path_map[f"{LOCAL_REPOS_DIR}/{name}"] = remote_path

        preview_url = self._maybe_create_preview(sandbox)
        return {
            "external_id": str(getattr(sandbox, "id", "")),
            "preview_url": preview_url,
            "metadata": {
                "sandbox_name": getattr(sandbox, "name", sandbox_name),
                "repo_path_map": repo_path_map,
                "repositories": [
                    {
                        "repo_url": _safe_repo_url(repo_url),
                        "local_path": f"{LOCAL_REPOS_DIR}/{_repo_name(repo_url)}",
                        "remote_path": f"{WORKSPACE_DIR}/{_repo_name(repo_url)}",
                    }
                    for repo_url in repo_urls
                ],
                "workspace_dir": WORKSPACE_DIR,
                "base_image": os.environ.get(
                    "DAYTONA_SANDBOX_BASE_IMAGE",
                    "mcr.microsoft.com/playwright:v1.51.1-noble",
                ),
            },
        }

    @staticmethod
    def _build_image(image_cls: Any) -> Any:
        base_image = os.environ.get(
            "DAYTONA_SANDBOX_BASE_IMAGE",
            "mcr.microsoft.com/playwright:v1.51.1-noble",
        )
        return (
            image_cls.base(base_image)
            .dockerfile_commands(
                [
                    "RUN apt-get update && apt-get install -y git curl bash ca-certificates openssh-client procps util-linux && rm -rf /var/lib/apt/lists/*",
                    "RUN curl -LsSf https://astral.sh/uv/install.sh | sh && ln -sf /root/.local/bin/uv /usr/local/bin/uv",
                    "RUN corepack enable || true",
                ]
            )
            .workdir(WORKSPACE_DIR)
        )

    @staticmethod
    def _build_resources(sdk: SimpleNamespace) -> Any | None:
        requested: dict[str, int] = {}
        for resource_name, env_name in (
            ("cpu", "DAYTONA_SANDBOX_CPU"),
            ("memory", "DAYTONA_SANDBOX_MEMORY"),
            ("disk", "DAYTONA_SANDBOX_DISK"),
            ("gpu", "DAYTONA_SANDBOX_GPU"),
        ):
            raw = os.environ.get(env_name)
            if raw:
                requested[resource_name] = int(raw)
        if not requested:
            return None
        resources_cls = getattr(sdk, "Resources", None)
        if resources_cls is None:
            raise DaytonaSandboxError("Installed Daytona SDK does not support sandbox resources.")
        return resources_cls(**requested)

    @staticmethod
    def _ensure_started(sandbox: Any) -> None:
        state = str(getattr(sandbox, "state", "") or "").lower()
        if state and state != "started":
            sandbox.start(timeout=120)

    def _exec_sync(
        self,
        sandbox_id: str,
        command: str,
        *,
        cwd: str | None = None,
        timeout: int = 30,
        env: dict[str, str] | None = None,
    ) -> Any:
        sandbox = self._client().get(sandbox_id)
        self._ensure_started(sandbox)
        return sandbox.process.exec(command, cwd=cwd, env=env, timeout=timeout)

    def _clone_repository(self, sandbox: Any, repo_url: str, remote_path: str) -> None:
        github_token = os.environ.get("GITHUB_TOKEN")
        env: dict[str, str] | None = None
        setup_prefix = ""
        if github_token and repo_url.startswith("https://") and "github.com" in repo_url:
            askpass_path = f"/tmp/hobbes-git-askpass-{uuid.uuid4().hex}.sh"
            askpass_script = """#!/bin/sh
case "$1" in
  *Username*) printf '%s\\n' "$GIT_USERNAME" ;;
  *Password*) printf '%s\\n' "$GIT_PASSWORD" ;;
  *) printf '\\n' ;;
esac
"""
            encoded = base64.b64encode(askpass_script.encode("utf-8")).decode("ascii")
            setup_prefix = (
                f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(askpass_path)} && "
                f"chmod 700 {shlex.quote(askpass_path)} && "
            )
            env = {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": askpass_path,
                "GIT_USERNAME": "x-access-token",
                "GIT_PASSWORD": github_token,
            }

        clone_parts = ["git", "clone", "--depth", "1", repo_url, remote_path]
        clone_command = " ".join(shlex.quote(part) for part in clone_parts)
        script = (
            f"set -e\n"
            f"mkdir -p {shlex.quote(WORKSPACE_DIR)}\n"
            f"if test -d {shlex.quote(remote_path)}/.git; then exit 0; fi\n"
            f"rm -rf {shlex.quote(remote_path)}\n"
            f"{setup_prefix}{clone_command}"
        )
        response = sandbox.process.exec(
            f"bash -lc {shlex.quote(script)}",
            cwd=WORKSPACE_DIR,
            env=env,
            timeout=int(os.environ.get("DAYTONA_SANDBOX_CLONE_TIMEOUT_SECONDS", "600")),
        )
        if _response_exit_code(response) != 0:
            detail = _response_text(response).replace(github_token or "", "***")
            raise DaytonaSandboxError(
                f"Failed to clone {_safe_repo_url(repo_url)} into Daytona sandbox: {detail}"
            )

    @staticmethod
    def _maybe_create_preview(sandbox: Any) -> str | None:
        raw_port = os.environ.get("DAYTONA_SANDBOX_PREVIEW_PORT")
        if not raw_port:
            return None
        port = int(raw_port)
        preview = sandbox.create_signed_preview_url(
            port,
            expires_in_seconds=int(os.environ.get("DAYTONA_SANDBOX_PREVIEW_TTL_SECONDS", "3600")),
        )
        return str(getattr(preview, "url", "") or "")

    @staticmethod
    def _remote_cwd(cwd: str, sandbox_run: dict) -> str:
        if not cwd:
            return WORKSPACE_DIR
        if cwd.startswith(f"{WORKSPACE_DIR}/") or cwd == WORKSPACE_DIR:
            return cwd
        repo_path_map = (sandbox_run.get("metadata") or {}).get("repo_path_map") or {}
        for local_path, remote_path in sorted(repo_path_map.items(), key=lambda item: len(item[0]), reverse=True):
            if cwd == local_path:
                return str(remote_path)
            if cwd.startswith(f"{local_path}/"):
                return f"{remote_path}{cwd[len(local_path):]}"
        if cwd.startswith(f"{LOCAL_REPOS_DIR}/"):
            return f"{WORKSPACE_DIR}{cwd[len(LOCAL_REPOS_DIR):]}"
        if cwd.startswith("/"):
            return cwd
        return f"{WORKSPACE_DIR}/{cwd.strip('/')}"

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
        sandbox_id = sandbox_run.get("external_id")
        if not sandbox_id:
            raise DaytonaSandboxError("Daytona sandbox run has no external_id.")

        remote_cwd = self._remote_cwd(cwd, sandbox_run)
        command_run = await create_sandbox_command_run(
            sandbox_run_id=sandbox_run["id"],
            session_id=session_id,
            command=command,
            cwd=cwd,
            run_kind="command",
            metadata={
                "runner_provider": self.provider,
                "remote_cwd": remote_cwd,
            },
        )
        start = time.monotonic()
        timed_out = False
        stdout = ""
        stderr = ""
        exit_code: int | None = None
        try:
            response = await asyncio.to_thread(
                self._exec_sync,
                sandbox_id,
                f"bash -lc {shlex.quote(command)}",
                cwd=remote_cwd,
                timeout=max(1, min(int(timeout_seconds), 600)),
            )
            exit_code = _response_exit_code(response)
            stdout = _response_text(response)
        except Exception as exc:
            message = str(exc)
            timed_out = "timeout" in message.lower() or "timed out" in message.lower()
            stderr = message

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
        sandbox_id = sandbox_run.get("external_id")
        if not sandbox_id:
            raise DaytonaSandboxError("Daytona sandbox run has no external_id.")
        remote_cwd = self._remote_cwd(cwd, sandbox_run)
        handle = str(uuid.uuid4())
        log_path = f"/tmp/hobbes-sandbox-{handle}.log"
        pid_path = f"/tmp/hobbes-sandbox-{handle}.pid"
        command_run = await create_sandbox_command_run(
            sandbox_run_id=sandbox_run["id"],
            session_id=session_id,
            command=command,
            cwd=cwd,
            run_kind="background_process",
            process_handle=handle,
            metadata={
                "runner_provider": self.provider,
                "remote_cwd": remote_cwd,
                "log_path": log_path,
                "pid_path": pid_path,
            },
        )
        launch_script = (
            "set -e\n"
            f"mkdir -p {shlex.quote(log_path.rsplit('/', 1)[0])}\n"
            f": > {shlex.quote(log_path)}\n"
            f"cd {shlex.quote(remote_cwd)}\n"
            f"nohup bash -lc {shlex.quote(command)} > {shlex.quote(log_path)} 2>&1 < /dev/null &\n"
            "pid=$!\n"
            f"echo \"$pid\" > {shlex.quote(pid_path)}\n"
            "sleep 0.2\n"
            "if kill -0 \"$pid\" 2>/dev/null; then echo \"$pid\"; else "
            f"cat {shlex.quote(log_path)}; exit 1; fi"
        )
        response = await asyncio.to_thread(
            self._exec_sync,
            sandbox_id,
            f"bash -lc {shlex.quote(launch_script)}",
            cwd=WORKSPACE_DIR,
            timeout=10,
        )
        exit_code = _response_exit_code(response)
        output = _response_text(response).strip()
        if exit_code != 0:
            await update_sandbox_command_run(
                command_run["id"],
                status="failed",
                exit_code=exit_code,
                stdout_tail=tail_lines(output, 200),
                stderr_tail="",
            )
            raise DaytonaSandboxError(f"Failed to start background process: {output}")
        pid = int(output.splitlines()[-1].strip())
        await update_sandbox_command_run(
            command_run["id"],
            pid=pid,
            metadata={"pid": pid},
        )
        return BackgroundStartResult(
            sandbox_run_id=sandbox_run["id"],
            command_run_id=command_run["id"],
            handle=handle,
            pid=pid,
            command=command,
            cwd=cwd,
            name=name or command[:40],
        )

    async def _background_context(self, session_id: str, handle: str) -> tuple[dict, dict]:
        command_run = await get_sandbox_command_run_by_handle(session_id, handle)
        if command_run is None:
            raise ValueError(f"no background process with handle={handle!r}")
        sandbox_run = await get_sandbox_run(command_run["sandbox_run_id"])
        if sandbox_run is None:
            raise ValueError(f"sandbox run {command_run['sandbox_run_id']} no longer exists")
        if sandbox_run.get("provider") != self.provider:
            raise ValueError(f"handle {handle!r} belongs to provider {sandbox_run.get('provider')!r}")
        if not sandbox_run.get("external_id"):
            raise DaytonaSandboxError("Daytona sandbox run has no external_id.")
        return sandbox_run, command_run

    async def read_background_process_output(
        self,
        *,
        session_id: str,
        handle: str,
        tail_lines_count: int = 200,
    ) -> BackgroundOutputResult:
        sandbox_run, command_run = await self._background_context(session_id, handle)
        metadata = command_run.get("metadata") or {}
        log_path = metadata.get("log_path")
        pid_path = metadata.get("pid_path")
        if not log_path or not pid_path:
            raise DaytonaSandboxError(f"background process {handle!r} is missing log metadata")
        tail_count = max(1, min(int(tail_lines_count), BG_LOG_MAX_LINES))
        script = (
            f"pid=$(cat {shlex.quote(pid_path)} 2>/dev/null || true)\n"
            "if test -n \"$pid\" && kill -0 \"$pid\" 2>/dev/null; then "
            "echo __HOBBES_STATUS__:running:$pid; "
            "else echo __HOBBES_STATUS__:exited:${pid:-unknown}; fi\n"
            "echo __HOBBES_OUTPUT__\n"
            f"test -f {shlex.quote(log_path)} && tail -n {tail_count} {shlex.quote(log_path)} || true"
        )
        response = await asyncio.to_thread(
            self._exec_sync,
            sandbox_run["external_id"],
            f"bash -lc {shlex.quote(script)}",
            cwd=WORKSPACE_DIR,
            timeout=30,
        )
        output = _response_text(response)
        status_line, _, body = output.partition("__HOBBES_OUTPUT__")
        status_marker = next(
            (line for line in status_line.splitlines() if line.startswith("__HOBBES_STATUS__:")),
            "__HOBBES_STATUS__:unknown",
        )
        parts = status_marker.split(":", 2)
        status_value = parts[1] if len(parts) > 1 else "unknown"
        pid = parts[2] if len(parts) > 2 else str(command_run.get("pid") or "unknown")
        status = f"running (pid={pid})" if status_value == "running" else "exited"
        lines = body.lstrip("\n").splitlines()
        await update_sandbox_command_run(
            command_run["id"],
            status="failed" if status_value == "exited" and command_run["status"] == "running" else None,
            stdout_tail="\n".join(lines),
        )
        return BackgroundOutputResult(
            sandbox_run_id=sandbox_run["id"],
            command_run_id=command_run["id"],
            handle=handle,
            command=command_run["command"],
            cwd=command_run.get("cwd") or "",
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
        sandbox_run, command_run = await self._background_context(session_id, handle)
        metadata = command_run.get("metadata") or {}
        log_path = metadata.get("log_path")
        pid_path = metadata.get("pid_path")
        if not log_path or not pid_path:
            raise DaytonaSandboxError(f"background process {handle!r} is missing log metadata")
        grace_seconds = max(0, min(int(grace_seconds), 60))
        tenths = max(1, grace_seconds * 10)
        script = (
            "signal=none\n"
            "exit_code=\n"
            f"pid=$(cat {shlex.quote(pid_path)} 2>/dev/null || true)\n"
            "if test -n \"$pid\" && kill -0 \"$pid\" 2>/dev/null; then\n"
            "  signal=SIGTERM\n"
            "  kill -TERM \"$pid\" 2>/dev/null || true\n"
            f"  for i in $(seq 1 {tenths}); do kill -0 \"$pid\" 2>/dev/null || break; sleep 0.1; done\n"
            "  if kill -0 \"$pid\" 2>/dev/null; then signal='SIGTERM then SIGKILL'; kill -KILL \"$pid\" 2>/dev/null || true; exit_code=-9; else exit_code=-15; fi\n"
            "fi\n"
            "echo __HOBBES_SIGNAL__:$signal\n"
            "echo __HOBBES_EXIT_CODE__:${exit_code:-}\n"
            "echo __HOBBES_OUTPUT__\n"
            f"test -f {shlex.quote(log_path)} && tail -n 20 {shlex.quote(log_path)} || true"
        )
        response = await asyncio.to_thread(
            self._exec_sync,
            sandbox_run["external_id"],
            f"bash -lc {shlex.quote(script)}",
            cwd=WORKSPACE_DIR,
            timeout=max(10, grace_seconds + 15),
        )
        output = _response_text(response)
        header, _, body = output.partition("__HOBBES_OUTPUT__")
        signal_line = next(
            (line for line in header.splitlines() if line.startswith("__HOBBES_SIGNAL__:")),
            "__HOBBES_SIGNAL__:none",
        )
        exit_line = next(
            (line for line in header.splitlines() if line.startswith("__HOBBES_EXIT_CODE__:")),
            "__HOBBES_EXIT_CODE__:",
        )
        signal_used = signal_line.split(":", 1)[1] or "none"
        exit_raw = exit_line.split(":", 1)[1].strip()
        exit_code = int(exit_raw) if exit_raw else None
        lines = body.lstrip("\n").splitlines()
        await update_sandbox_command_run(
            command_run["id"],
            status="cancelled" if signal_used != "none" else None,
            exit_code=exit_code,
            stdout_tail="\n".join(lines),
            stderr_tail="",
            metadata={"stop_signal": signal_used},
        )
        return BackgroundStopResult(
            sandbox_run_id=sandbox_run["id"],
            command_run_id=command_run["id"],
            handle=handle,
            command=command_run["command"],
            signal_used=signal_used,
            exit_code=exit_code,
            lines=lines,
        )


_local_runner = LocalSandboxRunner()
_daytona_runner = DaytonaSandboxRunner()


def get_sandbox_runner() -> SandboxRunner:
    provider = os.environ.get("SANDBOX_RUNNER_PROVIDER", "local").strip().lower()
    if provider == "local":
        return _local_runner
    if provider == "daytona":
        return _daytona_runner
    raise ValueError(f"Unknown SANDBOX_RUNNER_PROVIDER={provider!r}")
