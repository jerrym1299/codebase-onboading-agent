"""
Deletion helpers for sessions and per-repo artifacts.

Each step is independent and isolated in its own try/except, so partial
pipeline state (rows in some tables but not others) is safely cleaned up
without one missing piece blocking the rest.

A DeletionReport is returned summarising rows deleted, steps skipped
(because there was nothing to delete), and any per-step errors.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from temporalio.client import Client
from temporalio.service import RPCError

from services.db import get_pool

logger = logging.getLogger(__name__)

AGENT_SESSION_DB = os.environ.get("AGENT_SESSION_DB", "agent_sessions.db")
REPOS_BASE_DIR = "/repos"


@dataclass
class DeletionReport:
    """What got deleted, what was a no-op, what blew up. Always serialisable."""
    target: str
    deleted: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "deleted": self.deleted,
            "skipped": self.skipped,
            "errors": self.errors,
        }

    async def astep(self, key: str, coro_factory: Callable[[], Awaitable[int]]) -> None:
        try:
            n = await coro_factory()
            if n == 0:
                self.skipped.append(key)
            else:
                self.deleted[key] = n
        except Exception as exc:
            logger.exception("delete step %s failed: %s", key, exc)
            self.errors[key] = str(exc)[:500]

    def step(self, key: str, fn: Callable[[], int]) -> None:
        try:
            n = fn()
            if n == 0:
                self.skipped.append(key)
            else:
                self.deleted[key] = n
        except Exception as exc:
            logger.exception("delete step %s failed: %s", key, exc)
            self.errors[key] = str(exc)[:500]


def _local_clone_path(repo_url: str) -> Path:
    repo_name = repo_url.rstrip("/").removesuffix(".git").split("/")[-1]
    return Path(REPOS_BASE_DIR) / repo_name


async def _exec(sql: str, args: tuple) -> int:
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, args)
        return cur.rowcount


async def delete_repo_data(
    repo_url: str,
    *,
    delete_clone: bool = False,
) -> DeletionReport:
    """Delete all per-repo rows: code_chunks, dir_summaries, startup_plans,
    repo_boundaries. Does NOT touch app_startup_plans (keyed by repo_set_hash;
    callers handle that separately so they don't yank rows shared with other
    sessions). Does NOT touch the local clone unless delete_clone=True.

    Safe to call against a partially-indexed or fully-missing repo.
    """
    repo_url = repo_url.rstrip("/")
    report = DeletionReport(target=f"repo:{repo_url}")

    await report.astep(
        "code_chunks",
        lambda: _exec("DELETE FROM code_chunks WHERE repo_url = %s", (repo_url,)),
    )
    await report.astep(
        "dir_summaries",
        lambda: _exec("DELETE FROM dir_summaries WHERE repo_url = %s", (repo_url,)),
    )
    await report.astep(
        "startup_plans",
        lambda: _exec("DELETE FROM startup_plans WHERE repo_url = %s", (repo_url,)),
    )
    await report.astep(
        "repo_boundaries",
        lambda: _exec("DELETE FROM repo_boundaries WHERE repo_url = %s", (repo_url,)),
    )

    if delete_clone:
        def _rm() -> int:
            path = _local_clone_path(repo_url)
            if not path.is_dir():
                return 0
            shutil.rmtree(path)
            return 1
        report.step("clone_dir", _rm)

    return report


async def delete_app_plan_data(repo_set_hash: str) -> DeletionReport:
    """Delete the consolidated app-level plan for a given repo set."""
    report = DeletionReport(target=f"app_plan:{repo_set_hash}")
    await report.astep(
        "app_startup_plans",
        lambda: _exec(
            "DELETE FROM app_startup_plans WHERE repo_set_hash = %s",
            (repo_set_hash,),
        ),
    )
    return report


def _clear_agent_sessions_db(session_id: str) -> int:
    """Best-effort delete of the openai-agents SQLiteSession rows for one session.
    Schema is owned by the SDK so we scan tables that have a session_id column."""
    if not os.path.exists(AGENT_SESSION_DB):
        return 0
    deleted = 0
    with sqlite3.connect(AGENT_SESSION_DB) as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        for table in tables:
            try:
                cur.execute(f"PRAGMA table_info({table})")
                cols = {row[1] for row in cur.fetchall()}
                if "session_id" not in cols:
                    continue
                cur.execute(
                    f"DELETE FROM {table} WHERE session_id = ?", (session_id,)
                )
                deleted += cur.rowcount or 0
            except sqlite3.OperationalError:
                continue
        conn.commit()
    return deleted


async def _terminate_workflow(client: Client, session_id: str) -> int:
    handle = client.get_workflow_handle(f"chat-{session_id}")
    try:
        await handle.terminate(reason="session deleted")
        return 1
    except RPCError:
        return 0


async def delete_session_data(
    session_id: str,
    *,
    temporal_client: Client | None = None,
    cascade_orphan_repos: bool = True,
    delete_clones: bool = False,
) -> DeletionReport:
    """Delete a session and its dependents.

    Steps (each isolated; one failure doesn't block the others):
      1. Snapshot the session's repo_urls and app_plan_hash before deletion.
      2. Terminate the Temporal workflow if still running.
      3. DELETE from sessions (CASCADE handles session_repos, messages,
         pending_actions via foreign keys).
      4. Clear the SDK's SQLite agent session.
      5. If cascade_orphan_repos: for each repo no longer referenced by any
         session, delete per-repo data (code_chunks, dir_summaries,
         startup_plans, repo_boundaries; optionally the local clone).
      6. If the snapshotted app_plan_hash has no remaining sessions, delete
         the app_startup_plans row.
    """
    report = DeletionReport(target=f"session:{session_id}")
    pool = await get_pool()

    repo_urls: list[str] = []
    app_plan_hash: str | None = None

    # Step 1 — snapshot
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT app_plan_hash FROM sessions WHERE id = %s",
                (session_id,),
            )
            row = await cur.fetchone()
            if row is None:
                report.skipped.append("session_not_found")
            else:
                app_plan_hash = row[0]
            await cur.execute(
                "SELECT repo_url FROM session_repos WHERE session_id = %s",
                (session_id,),
            )
            repo_urls = [r[0] for r in await cur.fetchall()]
    except Exception as exc:
        logger.exception("snapshot failed: %s", exc)
        report.errors["snapshot"] = str(exc)[:500]

    # Step 2 — terminate workflow
    if temporal_client is not None:
        try:
            n = await _terminate_workflow(temporal_client, session_id)
            if n:
                report.deleted["workflow_terminated"] = 1
            else:
                report.skipped.append("workflow_already_closed")
        except Exception as exc:
            logger.exception("workflow termination failed: %s", exc)
            report.errors["workflow"] = str(exc)[:500]
    else:
        report.skipped.append("workflow_no_client")

    # Step 3 — delete session row (cascades to session_repos, messages, pending_actions)
    await report.astep(
        "sessions",
        lambda: _exec("DELETE FROM sessions WHERE id = %s", (session_id,)),
    )

    # Step 4 — SQLite agent session
    report.step("agent_sessions_sqlite", lambda: _clear_agent_sessions_db(session_id))

    # Step 5 — cascade orphaned per-repo data
    if cascade_orphan_repos:
        for url in repo_urls:
            try:
                async with pool.connection() as conn, conn.cursor() as cur:
                    await cur.execute(
                        "SELECT count(*) FROM session_repos WHERE repo_url = %s",
                        (url,),
                    )
                    remaining = (await cur.fetchone())[0]
                if remaining > 0:
                    report.skipped.append(f"repo_still_referenced:{url}")
                    continue
                sub = await delete_repo_data(url, delete_clone=delete_clones)
                for k, v in sub.deleted.items():
                    report.deleted[f"{url}::{k}"] = v
                for k in sub.skipped:
                    report.skipped.append(f"{url}::{k}")
                for k, v in sub.errors.items():
                    report.errors[f"{url}::{k}"] = v
            except Exception as exc:
                logger.exception("cascade for %s failed: %s", url, exc)
                report.errors[f"repo:{url}"] = str(exc)[:500]

    # Step 6 — delete the app-level plan if orphaned
    if app_plan_hash:
        async def _del_app_plan() -> int:
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT count(*) FROM sessions WHERE app_plan_hash = %s",
                    (app_plan_hash,),
                )
                remaining = (await cur.fetchone())[0]
                if remaining > 0:
                    return 0
                await cur.execute(
                    "DELETE FROM app_startup_plans WHERE repo_set_hash = %s",
                    (app_plan_hash,),
                )
                return cur.rowcount
        await report.astep("app_startup_plans", _del_app_plan)

    return report
