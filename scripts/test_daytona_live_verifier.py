"""Live smoke test for the Verifier agent using Daytona provider.

This makes an actual LLM-backed Verifier run call `run_shell`, which provisions
a real Daytona sandbox, clones a tiny public repo, executes the command, and
persists command history. The remote sandbox and local DB rows are cleaned up.

Usage:
    SANDBOX_RUNNER_PROVIDER=daytona python3 scripts/test_daytona_live_verifier.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents import Runner

from agent_defs import verifier_agent
from services.db import (
    close_pool,
    get_pool,
    init_schema,
    insert_session_repos,
    list_sandbox_command_runs,
    list_sandbox_runs_for_session,
    update_sandbox_run,
)
from services.tools import current_session_id


REPO_URL = "https://github.com/octocat/Hello-World"


def assert_true(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"  ok: {label}")


async def create_session() -> str:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO sessions (status) VALUES ('daytona_live_verifier_smoke') RETURNING id"
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


def delete_daytona_sandbox(sandbox_id: str) -> None:
    from daytona import Daytona

    sandbox = Daytona().get(sandbox_id)
    sandbox.delete(timeout=120)


async def main() -> None:
    assert_true(bool(os.environ.get("DAYTONA_API_KEY")), "DAYTONA_API_KEY is present")
    assert_true(bool(os.environ.get("OPENAI_API_KEY")), "OPENAI_API_KEY is present")
    assert_true(os.environ.get("SANDBOX_RUNNER_PROVIDER") == "daytona", "provider is Daytona")
    await init_schema()

    session_id = await create_session()
    token = current_session_id.set(session_id)
    sandbox_id = None
    sandbox_run_id = None
    print(f"SESSION_ID={session_id}")

    try:
        prompt = (
            "This is a live Daytona sandbox integration smoke test. You must call "
            "`run_shell` exactly once with command `pwd && printf 'live-verifier-ok\\n'`, "
            "cwd `/repos/Hello-World`, timeout_seconds=60, and max_output_lines=80. "
            "Then report the command result, including any sandbox_run_id and "
            "command_run_id shown by the tool. Do not start a background process."
        )
        result = await Runner.run(verifier_agent, prompt, max_turns=5)
        final = result.final_output or ""
        print("FINAL_OUTPUT_START")
        print(final)
        print("FINAL_OUTPUT_END")

        runs = await list_sandbox_runs_for_session(session_id)
        assert_true(len(runs) == 1, "Verifier created one live Daytona sandbox run")
        sandbox_run = runs[0]
        sandbox_run_id = sandbox_run["id"]
        sandbox_id = sandbox_run["external_id"]
        print(f"SANDBOX_RUN_ID={sandbox_run_id}")
        print(f"DAYTONA_SANDBOX_ID={sandbox_id}")
        assert_true(bool(sandbox_id), "Daytona sandbox external_id persisted")
        assert_true(
            sandbox_run["metadata"]["repo_path_map"].get("/repos/Hello-World")
            == "/workspace/Hello-World",
            "repo path mapping persisted",
        )

        commands = await list_sandbox_command_runs(sandbox_run_id)
        assert_true(len(commands) == 1, "Verifier persisted one command run")
        command = commands[0]
        print(f"COMMAND_RUN_ID={command['id']}")
        print(f"COMMAND_STATUS={command['status']}")
        print("COMMAND_STDOUT_TAIL_START")
        print(command.get("stdout_tail") or "")
        print("COMMAND_STDOUT_TAIL_END")
        assert_true(command["status"] == "complete", "Verifier command completed")
        assert_true(
            "/workspace/Hello-World" in (command.get("stdout_tail") or ""),
            "Verifier command ran in Daytona workspace",
        )
        assert_true(
            "live-verifier-ok" in (command.get("stdout_tail") or ""),
            "Verifier command stdout persisted",
        )
        assert_true(
            command["metadata"].get("remote_cwd") == "/workspace/Hello-World",
            "remote cwd persisted",
        )
        print("\nLIVE DAYTONA VERIFIER SMOKE PASSED")
    finally:
        current_session_id.reset(token)
        if sandbox_id:
            try:
                await asyncio.to_thread(delete_daytona_sandbox, sandbox_id)
                if sandbox_run_id:
                    await update_sandbox_run(
                        sandbox_run_id,
                        status="cancelled",
                        metadata={"live_verifier_smoke_deleted": True},
                    )
                print("  ok: deleted live Daytona sandbox")
            except Exception as exc:
                print(f"WARNING: failed to delete live Daytona sandbox {sandbox_id}: {exc}")
        await cleanup_session(session_id)
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
