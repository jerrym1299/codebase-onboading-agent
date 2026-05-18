"""Smoke tests for the sandbox runner execution substrate.

Exercises one-shot commands, persisted command history, background process
output, and process cleanup against the local Postgres-backed app database.

Usage:
    python3 scripts/test_sandbox_runner.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from services.db import (
    close_pool,
    get_pool,
    init_schema,
    list_sandbox_command_runs,
    list_sandbox_runs_for_session,
)
from services.sandbox_runner import LocalSandboxRunner


def assert_true(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"  ok: {label}")


async def _create_session() -> str:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO sessions (status) VALUES ('sandbox_smoke') RETURNING id"
            )
            row = await cur.fetchone()
    return str(row[0])


async def _cleanup_session(session_id: str) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM sandbox_runs WHERE session_id = %s", (session_id,))
            await cur.execute("DELETE FROM sessions WHERE id = %s", (session_id,))


async def main() -> None:
    await init_schema()
    session_id = await _create_session()
    runner = LocalSandboxRunner()
    print(f"created session {session_id}")

    try:
        print("testing one-shot command")
        result = await runner.run_command(
            session_id=session_id,
            command="printf 'hello sandbox\\n'",
            timeout_seconds=5,
            max_output_lines=20,
        )
        assert_true(result.exit_code == 0, "one-shot command exits cleanly")
        assert_true("hello sandbox" in result.stdout, "stdout is captured")

        print("testing timeout persistence")
        timed_out = await runner.run_command(
            session_id=session_id,
            command="sleep 2",
            timeout_seconds=1,
            max_output_lines=20,
        )
        assert_true(timed_out.timed_out, "timeout is reported")
        assert_true(timed_out.exit_code != 0, "timed-out command has non-zero exit")

        print("testing background process lifecycle")
        bg = await runner.start_background_process(
            session_id=session_id,
            command=(
                "python3 -c \"import time; "
                "print('background ready', flush=True); time.sleep(30)\""
            ),
            name="smoke-bg",
        )
        output = None
        for _ in range(30):
            output = await runner.read_background_process_output(
                session_id=session_id,
                handle=bg.handle,
                tail_lines_count=20,
            )
            if any("background ready" in line for line in output.lines):
                break
            await asyncio.sleep(0.1)
        assert_true(
            output is not None and any("background ready" in line for line in output.lines),
            "background output is readable",
        )

        stopped = await runner.stop_background_process(
            session_id=session_id,
            handle=bg.handle,
            grace_seconds=1,
        )
        assert_true(stopped.signal_used in {"SIGTERM", "SIGTERM then SIGKILL"}, "process stops")

        print("testing persisted history")
        runs = await list_sandbox_runs_for_session(session_id)
        assert_true(len(runs) == 1, "one sandbox run is persisted")
        commands = await list_sandbox_command_runs(runs[0]["id"])
        assert_true(len(commands) == 3, "three command runs are persisted")
        statuses = {command["status"] for command in commands}
        assert_true("complete" in statuses, "complete command status persisted")
        assert_true("timed_out" in statuses, "timeout command status persisted")
        assert_true("cancelled" in statuses, "background stop status persisted")

        print("\nSANDBOX RUNNER SMOKE PASSED")
    finally:
        await _cleanup_session(session_id)
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
