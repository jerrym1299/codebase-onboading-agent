"""Live smoke test for the run_shell tool using Daytona provider.

This exercises the actual FunctionTool path used by the Verifier agent without
requiring an LLM call. It creates a real Daytona sandbox, runs one command via
`run_shell`, verifies persisted metadata, then deletes the remote sandbox and
local DB rows.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents.tool import ToolContext

from services.db import (
    close_pool,
    get_pool,
    init_schema,
    insert_session_repos,
    list_sandbox_runs_for_session,
    update_sandbox_run,
)
from services.tools import current_session_id, run_shell


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
                "INSERT INTO sessions (status) VALUES ('daytona_live_tool_smoke') RETURNING id"
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


async def invoke_run_shell(payload: dict) -> str:
    raw = json.dumps(payload)
    ctx = ToolContext(
        context=None,
        tool_name=run_shell.name,
        tool_call_id="daytona-live-tool-smoke",
        tool_arguments=raw,
    )
    return await run_shell.on_invoke_tool(ctx, raw)


async def main() -> None:
    assert_true(bool(os.environ.get("DAYTONA_API_KEY")), "DAYTONA_API_KEY is present")
    assert_true(os.environ.get("SANDBOX_RUNNER_PROVIDER") == "daytona", "provider is Daytona")
    await init_schema()

    session_id = await create_session()
    token = current_session_id.set(session_id)
    sandbox_id = None
    sandbox_run_id = None
    print(f"SESSION_ID={session_id}")

    try:
        output = await invoke_run_shell(
            {
                "command": "pwd && printf 'live-tool-ok\\n'",
                "cwd": "/repos/Hello-World",
                "timeout_seconds": 60,
                "max_output_lines": 80,
            }
        )
        print("TOOL_OUTPUT_START")
        print(output)
        print("TOOL_OUTPUT_END")
        assert_true("live-tool-ok" in output, "tool stdout returned")
        assert_true("/workspace/Hello-World" in output, "tool cwd mapped to Daytona workspace")
        assert_true("sandbox_run_id=" in output and "command_run_id=" in output, "tool returned persisted IDs")

        runs = await list_sandbox_runs_for_session(session_id)
        assert_true(len(runs) == 1, "one live Daytona tool sandbox run persisted")
        sandbox_run = runs[0]
        sandbox_run_id = sandbox_run["id"]
        sandbox_id = sandbox_run["external_id"]
        print(f"SANDBOX_RUN_ID={sandbox_run_id}")
        print(f"DAYTONA_SANDBOX_ID={sandbox_id}")
        assert_true(bool(sandbox_id), "Daytona sandbox external_id persisted")
        print("\nLIVE DAYTONA TOOL SMOKE PASSED")
    finally:
        current_session_id.reset(token)
        if sandbox_id:
            try:
                await asyncio.to_thread(delete_daytona_sandbox, sandbox_id)
                if sandbox_run_id:
                    await update_sandbox_run(
                        sandbox_run_id,
                        status="cancelled",
                        metadata={"live_tool_smoke_deleted": True},
                    )
                print("  ok: deleted live Daytona sandbox")
            except Exception as exc:
                print(f"WARNING: failed to delete live Daytona sandbox {sandbox_id}: {exc}")
        await cleanup_session(session_id)
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
