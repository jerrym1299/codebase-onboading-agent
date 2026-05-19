"""Smoke tests for DaytonaSandboxRunner using a fake Daytona SDK/client.

This does not call Daytona's network API. It validates our adapter behavior:
provisioning, session repo cloning intent, /repos -> /workspace cwd mapping,
command persistence, and remote background process log/pid handling.

Usage:
    python3 scripts/test_daytona_sandbox_runner.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from services.db import (
    close_pool,
    get_pool,
    init_schema,
    insert_session_repos,
    list_sandbox_command_runs,
    list_sandbox_runs_for_session,
)
from services.sandbox_runner import DaytonaSandboxRunner


REPO_URL = "https://github.com/acme/widget"


class FakeResponse:
    def __init__(self, exit_code: int = 0, result: str = "") -> None:
        self.exit_code = exit_code
        self.result = result


class FakeImage:
    @classmethod
    def base(cls, base_image: str):
        image = cls()
        image.base_image = base_image
        image.commands = []
        image.workdir_path = None
        return image

    def dockerfile_commands(self, commands: list[str]):
        self.commands = commands
        return self

    def workdir(self, path: str):
        self.workdir_path = path
        return self


class FakeParams:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class FakeResources:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class FakePreview:
    url = "https://fake-preview.example"


class FakeProcess:
    def __init__(self, sandbox: "FakeSandbox") -> None:
        self.sandbox = sandbox

    def exec(self, command: str, *, cwd=None, env=None, timeout=None) -> FakeResponse:
        self.sandbox.exec_calls.append(
            {"command": command, "cwd": cwd, "env": env, "timeout": timeout}
        )
        if "git clone" in command:
            return FakeResponse(0, "")
        if "daytona-ok" in command:
            self.sandbox.last_command_cwd = cwd
            return FakeResponse(0, "daytona-ok\n")
        if "definitely_missing_daytona_command" in command:
            return FakeResponse(127, "bash: definitely_missing_daytona_command: command not found\n")
        if "nohup bash -lc" in command:
            log_path = self._extract_path(command, ".log")
            pid_path = self._extract_path(command, ".pid")
            pid = "4242"
            self.sandbox.logs[log_path] = ["remote-ready"]
            self.sandbox.pids[pid_path] = {"pid": pid, "running": True, "log_path": log_path}
            return FakeResponse(0, pid + "\n")
        if "__HOBBES_STATUS__" in command:
            pid_state = next(iter(self.sandbox.pids.values()))
            status = "running" if pid_state["running"] else "exited"
            log = "\n".join(self.sandbox.logs[pid_state["log_path"]])
            return FakeResponse(
                0,
                f"__HOBBES_STATUS__:{status}:{pid_state['pid']}\n"
                f"__HOBBES_OUTPUT__\n{log}\n",
            )
        if "__HOBBES_SIGNAL__" in command:
            pid_state = next(iter(self.sandbox.pids.values()))
            pid_state["running"] = False
            log = "\n".join(self.sandbox.logs[pid_state["log_path"]])
            return FakeResponse(
                0,
                f"__HOBBES_SIGNAL__:SIGTERM\n"
                f"__HOBBES_EXIT_CODE__:-15\n"
                f"__HOBBES_OUTPUT__\n{log}\n",
            )
        return FakeResponse(0, "")

    @staticmethod
    def _extract_path(command: str, suffix: str) -> str:
        match = re.search(
            rf"/tmp/hobbes-sandbox-[A-Za-z0-9-]+{re.escape(suffix)}",
            command,
        )
        if not match:
            raise AssertionError(f"missing {suffix} path in command: {command}")
        return match.group(0)


class FakeSandbox:
    def __init__(self, sandbox_id: str = "fake-daytona-sandbox") -> None:
        self.id = sandbox_id
        self.name = "fake-daytona"
        self.state = "started"
        self.process = FakeProcess(self)
        self.exec_calls = []
        self.logs = {}
        self.pids = {}
        self.last_command_cwd = None

    def start(self, timeout: int = 120) -> None:
        self.state = "started"

    def create_signed_preview_url(self, port: int, *, expires_in_seconds: int) -> FakePreview:
        return FakePreview()


class FakeClient:
    def __init__(self) -> None:
        self.sandbox = FakeSandbox()
        self.create_calls = []

    def create(self, params, *, timeout=None):
        self.create_calls.append({"params": params, "timeout": timeout})
        return self.sandbox

    def get(self, sandbox_id: str):
        assert sandbox_id == self.sandbox.id
        return self.sandbox


def fake_sdk() -> SimpleNamespace:
    return SimpleNamespace(
        CreateSandboxFromImageParams=FakeParams,
        Daytona=lambda: FakeClient(),
        Image=FakeImage,
        Resources=FakeResources,
    )


def assert_true(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"  ok: {label}")


async def create_session() -> str:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO sessions (status) VALUES ('daytona_sandbox_smoke') RETURNING id"
            )
            row = await cur.fetchone()
    session_id = str(row[0])
    await insert_session_repos(session_id, [REPO_URL])
    return session_id


async def cleanup_session(session_id: str) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM sandbox_runs WHERE session_id = %s", (session_id,))
            await cur.execute("DELETE FROM session_repos WHERE session_id = %s", (session_id,))
            await cur.execute("DELETE FROM sessions WHERE id = %s", (session_id,))


async def main() -> None:
    await init_schema()
    session_id = await create_session()
    client = FakeClient()
    runner = DaytonaSandboxRunner(
        sdk_loader=fake_sdk,
        client_factory=lambda: client,
        repo_auth_tokens={REPO_URL: "repo-token-123"},
    )
    print(f"created session {session_id}")

    try:
        result = await runner.run_command(
            session_id=session_id,
            command="printf 'daytona-ok\\n'",
            cwd="/repos/widget",
            timeout_seconds=5,
            max_output_lines=20,
        )
        assert_true(result.exit_code == 0, "daytona one-shot command exits cleanly")
        assert_true("daytona-ok" in result.stdout, "daytona stdout is captured")
        assert_true(client.sandbox.last_command_cwd == "/workspace/widget", "cwd maps to workspace repo path")
        clone_call = next(call for call in client.sandbox.exec_calls if "git clone" in call["command"])
        assert_true(
            clone_call["env"]["GIT_PASSWORD"] == "repo-token-123",
            "repo-specific clone token is passed to Daytona",
        )

        failed = await runner.run_command(
            session_id=session_id,
            command="definitely_missing_daytona_command",
            cwd="/repos/widget",
            timeout_seconds=5,
            max_output_lines=20,
        )
        assert_true(failed.exit_code == 127, "daytona command failure is returned")

        bg = await runner.start_background_process(
            session_id=session_id,
            command="python3 -c \"print('remote-ready')\"",
            cwd="/repos/widget",
            name="fake-bg",
        )
        assert_true(bg.pid == 4242, "background pid is parsed")
        output = await runner.read_background_process_output(
            session_id=session_id,
            handle=bg.handle,
            tail_lines_count=20,
        )
        assert_true(output.status.startswith("running"), "background status is running")
        assert_true(output.lines == ["remote-ready"], "background output is tailed")
        stopped = await runner.stop_background_process(
            session_id=session_id,
            handle=bg.handle,
            grace_seconds=1,
        )
        assert_true(stopped.signal_used == "SIGTERM", "background stop signal is parsed")
        assert_true(stopped.exit_code == -15, "background stop exit code is parsed")

        runs = await list_sandbox_runs_for_session(session_id)
        assert_true(len(runs) == 1, "one Daytona sandbox run persisted")
        assert_true(runs[0]["external_id"] == "fake-daytona-sandbox", "external sandbox id persisted")
        assert_true(
            runs[0]["metadata"]["repo_path_map"]["/repos/widget"] == "/workspace/widget",
            "repo path map persisted",
        )
        commands = await list_sandbox_command_runs(runs[0]["id"])
        statuses = [command["status"] for command in commands]
        assert_true(statuses == ["complete", "failed", "cancelled"], "command statuses persisted")
        assert_true(
            commands[0]["metadata"]["remote_cwd"] == "/workspace/widget",
            "remote cwd persisted",
        )

        print("\nDAYTONA SANDBOX RUNNER SMOKE PASSED")
    finally:
        await cleanup_session(session_id)
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
