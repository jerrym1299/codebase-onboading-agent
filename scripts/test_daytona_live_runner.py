"""Live smoke test for DaytonaSandboxRunner.

This creates a real Daytona sandbox, clones a tiny public repo, executes one
command through the runner, verifies persisted metadata, then deletes the
remote sandbox and local DB rows.

Usage:
    python3 scripts/test_daytona_live_runner.py
    python3 scripts/test_daytona_live_runner.py --repo https://github.com/octocat/Hello-World
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from services.db import (
    close_pool,
    get_pool,
    init_schema,
    insert_session_repos,
    list_sandbox_command_runs,
    list_sandbox_runs_for_session,
    update_sandbox_run,
)
from services.sandbox_runner import DaytonaSandboxRunner


DEFAULT_REPO = "https://github.com/octocat/Hello-World"


def assert_true(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"  ok: {label}")


def repo_name(repo_url: str) -> str:
    return repo_url.rstrip("/").split("/")[-1].removesuffix(".git")


async def create_session(repo_url: str) -> str:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO sessions (status) VALUES ('daytona_live_smoke') RETURNING id"
            )
            row = await cur.fetchone()
    session_id = str(row[0])
    await insert_session_repos(session_id, [repo_url])
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=DEFAULT_REPO)
    args = parser.parse_args()

    assert_true(bool(os.environ.get("DAYTONA_API_KEY")), "DAYTONA_API_KEY is present")
    await init_schema()

    name = repo_name(args.repo)
    local_cwd = f"/repos/{name}"
    remote_cwd = f"/workspace/{name}"
    session_id = await create_session(args.repo)
    runner = DaytonaSandboxRunner()
    sandbox_id = None
    sandbox_run_id = None
    print(f"SESSION_ID={session_id}")

    try:
        result = await runner.run_command(
            session_id=session_id,
            command="pwd && ls -la && printf 'live-daytona-ok\\n'",
            cwd=local_cwd,
            timeout_seconds=60,
            max_output_lines=80,
        )
        print("COMMAND_STDOUT_START")
        print(result.stdout)
        print("COMMAND_STDOUT_END")
        assert_true(result.exit_code == 0, "live Daytona command exits cleanly")
        assert_true(remote_cwd in result.stdout, "cwd mapped into Daytona workspace")
        assert_true("live-daytona-ok" in result.stdout, "stdout returned from Daytona command")

        runs = await list_sandbox_runs_for_session(session_id)
        assert_true(len(runs) == 1, "one live Daytona sandbox run persisted")
        sandbox_run = runs[0]
        sandbox_run_id = sandbox_run["id"]
        sandbox_id = sandbox_run["external_id"]
        print(f"SANDBOX_RUN_ID={sandbox_run_id}")
        print(f"DAYTONA_SANDBOX_ID={sandbox_id}")
        assert_true(bool(sandbox_id), "Daytona sandbox external_id persisted")
        assert_true(
            sandbox_run["metadata"]["repo_path_map"].get(local_cwd) == remote_cwd,
            "repo path mapping persisted",
        )

        commands = await list_sandbox_command_runs(sandbox_run_id)
        assert_true(len(commands) == 1, "one command row persisted")
        assert_true(commands[0]["status"] == "complete", "command status persisted as complete")
        assert_true(commands[0]["metadata"].get("remote_cwd") == remote_cwd, "remote cwd persisted")
        print("\nLIVE DAYTONA RUNNER SMOKE PASSED")
    finally:
        if sandbox_id:
            try:
                await asyncio.to_thread(delete_daytona_sandbox, sandbox_id)
                if sandbox_run_id:
                    await update_sandbox_run(
                        sandbox_run_id,
                        status="cancelled",
                        metadata={"live_smoke_deleted": True},
                    )
                print("  ok: deleted live Daytona sandbox")
            except Exception as exc:
                print(f"WARNING: failed to delete live Daytona sandbox {sandbox_id}: {exc}")
        await cleanup_session(session_id)
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
