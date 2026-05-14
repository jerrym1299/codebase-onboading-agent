import asyncio
import contextvars
import json
import os
import re
import subprocess
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from agents import function_tool
from temporalio.client import Client

from services.chunk_and_embed import embed_query
from services.db import (
    CODE_SEARCH_SQL, DIR_SUMMARY_SEARCH_SQL, get_app_startup_plan_row, get_pool,
    get_repo_boundaries_row, get_startup_plan_row,
)

from services.event_bus import publish

current_session_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_session_id")


def _list_files(dir_path: str, glob: str = "**/*") -> list[str]:
    return [str(p) for p in Path(dir_path).glob(glob) if p.is_file()]


def _read_file(file_path: str, start_line: int = 0, end_line: int = -1) -> str:
    p = Path(file_path)
    if p.is_dir():
        return f"ERROR: {file_path} is a directory. Use list_files(dir_path, glob) instead."
    if not p.exists():
        return f"ERROR: {file_path} does not exist."
    with open(file_path, "r", errors="replace") as f:
        lines = f.readlines()
    return "".join(lines[start_line:] if end_line == -1 else lines[start_line:end_line])


@function_tool
def list_files(dir_path: str, glob: str = "**/*") -> list[str]:
    return _list_files(dir_path, glob)


@function_tool
def read_file(file_path: str, start_line: int = 0, end_line: int = -1) -> str:
    return _read_file(file_path, start_line, end_line)


@function_tool
def search_code(dir_path: str, query: str, file_type: str = "") -> list[str]:
    """Search code with regex or string. Returns 'file:line_number' for matches."""
    files = _list_files(dir_path, glob=f"**/*{file_type}" if file_type else "**/*")
    pattern = re.compile(query)
    results = []
    for file in files:
        try:
            content = _read_file(file)
        except (UnicodeDecodeError, OSError):
            continue
        for line_num, line in enumerate(content.splitlines(), start=1):
            if pattern.search(line):
                results.append(f"{file}:{line_num}")
    return results

@function_tool
def find_references(symbol:str, dir_path:str) -> list[str]:
    # Find references to a funciton or class name in the codebase
    files = _list_files(dir_path, glob="**/*")
    pattern = re.compile(symbol)
    results = []
    for file in files:
        try:
            content = _read_file(file)
        except (UnicodeDecodeError, OSError):
            continue
        for line_num, line in enumerate(content.splitlines(), start=1):
            if pattern.search(line):
                results.append(f"{file}:{line_num}")
    return results

_JS_IMPORT_PATTERNS = [
    re.compile(r"""import\s+.*?from\s+['"]([^'"]+)['"]"""),
    re.compile(r"""import\s+['"]([^'"]+)['"]"""),
    re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""import\(\s*['"]([^'"]+)['"]\s*\)"""),
]

_PY_IMPORT_PATTERNS = [
    re.compile(r"""^from\s+([\w.]+)\s+import""", re.MULTILINE),
    re.compile(r"""^import\s+([\w.]+)""", re.MULTILINE),
]


@function_tool
def get_dependencies(file_path: str) -> list[str]:
    """Extract import dependencies from a JS/TS/Python file."""
    p = Path(file_path)
    if not p.exists():
        return [f"ERROR: {file_path} does not exist."]
    if p.is_dir():
        return [f"ERROR: {file_path} is a directory."]
    try:
        with open(file_path, "r", errors="replace") as f:
            content = f.read()
    except OSError as e:
        return [f"ERROR: {e}"]

    ext = p.suffix.lower()
    patterns = _PY_IMPORT_PATTERNS if ext == ".py" else _JS_IMPORT_PATTERNS

    deps = []
    for pat in patterns:
        deps.extend(pat.findall(content))
    return sorted(set(deps))

@function_tool
async def search_indexed(query: str, repo_url: str, k: int = 10) -> str:
    """Search indexed chunks and find top-k relevant code chunks from pgvector."""
    emb = "[" + ",".join(repr(x) for x in embed_query(query)) + "]"
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(CODE_SEARCH_SQL, (emb, repo_url, emb, k))
        rows = await cur.fetchall()
    results = []
    for r in rows:
        results.append(
            f"[{r[6]:.3f}] {r[0]} ({r[1]}: {r[2]}) L{r[3]}-{r[4]}\n{r[5][:500]}"
        )
    return "\n---\n".join(results) if results else "No matching chunks found."


@function_tool
async def search_dir_summaries(query: str, repo_url: str, k: int = 5) -> str:
    """Search directory summaries by semantic similarity. Useful for understanding
    high-level project structure and what each directory is responsible for."""
    emb = "[" + ",".join(repr(x) for x in embed_query(query)) + "]"
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(DIR_SUMMARY_SEARCH_SQL, (emb, repo_url, emb, k))
        rows = await cur.fetchall()
    results = []
    for r in rows:
        files = ", ".join(r[2]) if r[2] else ""
        results.append(
            f"[{r[3]:.3f}] {r[0]}/\nFiles: {files}\n{r[1]}"
        )
    return "\n---\n".join(results) if results else "No directory summaries found."


@function_tool
def git_log(repo_dir: str, file_path: str = "", limit: int = 10) -> list[str]:
    """Run `git log` inside repo_dir. If file_path is given, scope the log
    to that file or directory (relative to repo_dir or absolute)."""
    cmd = ["git", "-C", repo_dir, "log", "--pretty=format:%h %s", "-n", str(limit)]
    if file_path:
        cmd += ["--", file_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return [f"ERROR: {result.stderr.strip() or 'git log failed'}"]
        return result.stdout.splitlines()
    except (subprocess.CalledProcessError, OSError) as e:
        return [f"ERROR: {e}"]


@function_tool
async def ask_user(question: str, options: list[str] | None = None) -> str:
    """Ask the user a clarifying question. Use when the query is ambiguous
    or you need the user to choose between alternatives before proceeding."""
    try:
        session_id = current_session_id.get()
    except LookupError:
        return "[ask_user unavailable: no active session context]"
    pending_id = str(uuid.uuid4())
    payload: dict = {"question": question}
    if options is not None:
        payload["options"] = options

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO pending_actions (id, session_id, kind, payload) "
            "VALUES (%s, %s, 'ask_user', %s::jsonb)",
            (pending_id, session_id, json.dumps(payload)),
        )

    await publish(session_id, {
        "type": "data-needs-input",
        "pendingId": pending_id,
        "question": question,
        "options": options,
    })

    return f"[Waiting for user response. Pending ID: {pending_id}]"


def _format_plan_for_llm(row: dict) -> str:
    plan = row.get("plan") or {}
    status = row.get("analysis_status")
    if status == "failed":
        return (
            f"Startup plan analysis FAILED for this repo "
            f"(error: {row.get('error', 'unknown')}). "
            "Investigate manually using list_files and read_file."
        )
    lines: list[str] = []
    lines.append(f"# Startup plan ({status}, confidence={row.get('overall_confidence')})")
    if plan.get("summary"):
        lines.append(plan["summary"])
    if row.get("truncations"):
        lines.append(f"_Note: dropped from context: {', '.join(row['truncations'])}_")
    for pkg in plan.get("packages", []):
        lines.append(f"\n## Package: {pkg.get('path', '.')} ({pkg.get('framework') or 'unknown'})")
        rt = pkg.get("runtime", {})
        lines.append(
            f"- Runtime: {rt.get('language')} {rt.get('version') or ''} "
            f"(source: {rt.get('version_source')}, conf {rt.get('confidence')})"
        )
        pm = pkg.get("package_manager", {})
        lines.append(
            f"- Package manager: {pm.get('name')} {pm.get('version') or ''} "
            f"(source: {pm.get('source')}, conf {pm.get('confidence')})"
        )
        if pkg.get("external_tools"):
            lines.append("- External tools:")
            for t in pkg["external_tools"]:
                req = "REQUIRED" if t.get("required") else "optional"
                lines.append(f"  - {t.get('name')} ({req}): {t.get('reason')}")
        if pkg.get("services"):
            lines.append("- Services:")
            for s in pkg["services"]:
                lines.append(
                    f"  - {s.get('name')} ({s.get('image') or '?'}, source {s.get('source')})"
                )
        if pkg.get("env_vars"):
            lines.append("- Env vars:")
            for e in pkg["env_vars"]:
                req = "REQUIRED" if e.get("required") else "optional"
                flag = " [needs verification]" if e.get("needs_verification") else ""
                lines.append(
                    f"  - {e.get('name')} ({req}, conf {e.get('confidence')}){flag}: "
                    f"example={e.get('example')!r} sources={e.get('sources') or []}"
                )
        if pkg.get("steps"):
            lines.append("- Steps:")
            for step in sorted(pkg["steps"], key=lambda s: s.get("order", 0)):
                flag = " [needs verification]" if step.get("needs_verification") else ""
                lines.append(
                    f"  {step.get('order')}. {step.get('title')}: "
                    f"`{step.get('command')}` (cwd={step.get('cwd')}, "
                    f"conf {step.get('confidence')}){flag}"
                )
                if step.get("explain"):
                    lines.append(f"     {step['explain']}")
    if plan.get("warnings"):
        lines.append("\n## Warnings")
        for w in plan["warnings"]:
            lines.append(f"- {w}")
    return "\n".join(lines)


@function_tool
async def get_startup_plan(repo_url: str) -> str:
    """Return the persisted startup plan for a repo, formatted for the LLM.
    Returns 'no plan available' when nothing has been computed yet."""
    row = await get_startup_plan_row(repo_url)
    if row is None:
        return "No startup plan available for this repo yet."
    return _format_plan_for_llm(row)


_temporal_client: Client | None = None


async def _temporal() -> Client:
    global _temporal_client
    if _temporal_client is None:
        _temporal_client = await Client.connect(
            os.environ.get("TEMPORAL_HOST", "temporal:7233")
        )
    return _temporal_client


@function_tool
async def recompute_startup_plan(repo_url: str, reason: str = "") -> str:
    """Signal the current session's workflow to recompute the startup plan."""
    try:
        session_id = current_session_id.get()
    except LookupError:
        return "[recompute_startup_plan unavailable: no active session context]"
    client = await _temporal()
    handle = client.get_workflow_handle(f"chat-{session_id}")
    await handle.signal("recompute_startup_plan", reason)
    return (
        f"Recompute requested for {repo_url}. "
        "The new plan will appear in a few seconds; re-call get_startup_plan to read it."
    )


@function_tool
async def get_repo_boundaries(repo_url: str) -> str:
    """Return the BoundaryReport JSON for a repo, or 'no boundaries available'."""
    row = await get_repo_boundaries_row(repo_url)
    if row is None:
        return "No boundary report available for this repo yet."
    return json.dumps({
        "report": row["report"],
        "analysis_status": row["analysis_status"],
        "model": row["model"],
    }, indent=2)


@function_tool
async def get_repo_startup_plan(repo_url: str) -> str:
    """Return the persisted startup plan for a repo, formatted for the LLM."""
    row = await get_startup_plan_row(repo_url)
    if row is None:
        return "No startup plan available for this repo yet."
    return _format_plan_for_llm(row)


@function_tool
async def get_app_startup_plan(session_id: str) -> str:
    """Return the consolidated app-level startup plan markdown for a session."""
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT app_plan_hash FROM sessions WHERE id = %s",
            (session_id,),
        )
        row = await cur.fetchone()
    if row is None or not row[0]:
        return "No app plan available for this session."
    plan = await get_app_startup_plan_row(row[0])
    if plan is None or not plan.get("plan_markdown"):
        return "No app plan available for this session."
    return plan["plan_markdown"]

UPDATE_APP_PLAN_MARKDOWN_SQL = """
    UPDATE app_startup_plans
       SET plan_markdown = %s,
           updated_at    = NOW()
     WHERE repo_set_hash = %s
"""


@function_tool
async def update_startup_plan(plan_markdown: str, change_summary: str = "") -> str:
    """Persist an updated app-level startup plan for the current session.

    Use this after gathering user feedback via `ask_user` to resolve ambiguities
    or incorporate corrections/updates from the user into the plan. Workflow:
      1. Read the current plan with `get_app_startup_plan(session_id)`.
      2. Identify ambiguities or fields the user might want to correct (env
         values, service ports, ordering, etc.). For each one, call `ask_user`
         to confirm or get a value.
      3. Construct the FULL updated markdown (replaces plan_markdown wholesale,
         keep the same six sections: Startup plan, Prerequisites, Env vars,
         Steps, Dependency graph, Caveats).
      4. Call this tool with the new markdown.

    `change_summary` is a one-line description of what changed, used only for
    the SSE event payload.
    """
    try:
        session_id = current_session_id.get()
    except LookupError:
        return "[update_startup_plan unavailable: no active session context]"

    if not plan_markdown or not plan_markdown.strip():
        return "ERROR: plan_markdown is empty; nothing to persist."

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT app_plan_hash FROM sessions WHERE id = %s",
            (session_id,),
        )
        row = await cur.fetchone()
        if row is None or not row[0]:
            return "ERROR: no app plan exists for this session yet."
        repo_set_hash = row[0]

        await cur.execute(UPDATE_APP_PLAN_MARKDOWN_SQL, (plan_markdown, repo_set_hash))
        if cur.rowcount == 0:
            return f"ERROR: no app_startup_plans row found for repo_set_hash={repo_set_hash}."

    fresh = await get_app_startup_plan_row(repo_set_hash)
    await publish(session_id, {
        "type": "data-app-plan-updated",
        "updatedAt": fresh["updated_at"] if fresh else None,
        "repo_set_hash": repo_set_hash,
        "change_summary": change_summary or None,
    })

    return f"App startup plan updated ({len(plan_markdown)} chars) for repo_set_hash={repo_set_hash}."


UPDATE_REPO_PLAN_SQL = """
    UPDATE startup_plans
       SET plan       = %s::jsonb,
           updated_at = NOW()
     WHERE repo_url = %s
"""


@function_tool
async def update_repo_startup_plan(
    repo_url: str,
    plan_json: str,
    change_summary: str = "",
) -> str:
    """Persist an updated per-repo startup plan for one repo in the current session.

    Use this for targeted corrections to a single repo's plan — fixing an env
    var value, adding a missing step, correcting a service port, etc. — instead
    of re-running the full analysis with `recompute_startup_plan`.

    Workflow:
      1. Read the current per-repo plan with `get_startup_plan(repo_url)`.
      2. For every ambiguity or missing value the user's change implies, call
         `ask_user` to confirm or get a value before guessing.
      3. Construct the FULL updated plan as a JSON object matching the existing
         schema (top-level keys: `summary`, `packages[]`, `warnings[]`; each
         package has `path`, `framework`, `runtime`, `package_manager`,
         `external_tools[]`, `services[]`, `env_vars[]`, `steps[]`). Preserve
         every field you aren't changing — this replaces `plan` wholesale.
      4. Serialise the plan to a JSON string and pass it as `plan_json`.
         `repo_url` must be one of the repos in this session.

    `change_summary` is a one-line description of what changed, used only for
    the SSE event payload.
    """
    try:
        session_id = current_session_id.get()
    except LookupError:
        return "[update_repo_startup_plan unavailable: no active session context]"

    if not repo_url or not repo_url.strip():
        return "ERROR: repo_url is empty."
    try:
        plan = json.loads(plan_json)
    except json.JSONDecodeError as e:
        return f"ERROR: plan_json is not valid JSON: {e}"
    if not isinstance(plan, dict) or not plan:
        return "ERROR: plan_json must decode to a non-empty JSON object."

    repo_url = repo_url.rstrip("/")

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM session_repos WHERE session_id = %s AND repo_url = %s",
            (session_id, repo_url),
        )
        if await cur.fetchone() is None:
            return (
                f"ERROR: repo_url {repo_url!r} is not part of this session. "
                "Use one of the repos listed in the developer prompt."
            )

        await cur.execute(UPDATE_REPO_PLAN_SQL, (json.dumps(plan), repo_url))
        if cur.rowcount == 0:
            return (
                f"ERROR: no startup_plans row exists for {repo_url}. "
                "Run `recompute_startup_plan` first to create one."
            )

    fresh = await get_startup_plan_row(repo_url)
    await publish(session_id, {
        "type": "data-startup-plan-updated",
        "updatedAt": fresh["updated_at"] if fresh else None,
        "repo_url": repo_url,
        "change_summary": change_summary or None,
    })

    return f"Startup plan updated for {repo_url}."


_OUTPUT_MAX_LINES_CAP = 5000


def _tail_lines(text: str, max_lines: int) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    kept = lines[-max_lines:]
    dropped = len(lines) - max_lines
    return f"[...truncated {dropped} earlier lines; showing last {max_lines}]\n" + "\n".join(kept)


def _format_shell_result(
    *,
    command: str,
    cwd: str,
    exit_code: int | None,
    duration_ms: int,
    timed_out: bool,
    stdout: str,
    stderr: str,
    max_output_lines: int,
) -> str:
    return (
        f"$ {command}\n"
        f"(cwd={cwd or '<default>'}, exit_code={exit_code}, "
        f"duration_ms={duration_ms}, timed_out={timed_out})\n"
        f"--- stdout ---\n{_tail_lines(stdout, max_output_lines)}\n"
        f"--- stderr ---\n{_tail_lines(stderr, max_output_lines)}"
    )


@function_tool
async def run_shell(
    command: str,
    cwd: str = "",
    timeout_seconds: int = 30,
    max_output_lines: int = 200,
) -> str:
    """Run a shell command inside the agent's container and return the result.

    Use for one-shot blocking commands needed to verify a startup plan —
    `pnpm install`, `pip install -r requirements.txt`, `curl localhost:3000`,
    `node --version`, etc. Commands run via `bash -c <command>` so pipes,
    redirects, env vars, and command chains all work.

    `cwd` should usually be the local repo path from the developer prompt
    (e.g. /repos/<repo_name>). Leave empty to use the container's default cwd.

    `timeout_seconds` is hard-capped to 600. Long-running dev servers will not
    return on their own — use `start_background_process` for those.

    `max_output_lines` caps how many of the *last* lines of stdout and stderr
    are returned (each stream is tailed independently). Defaults to 200,
    hard-capped at 5000. Bump it when you need to see more of a long install
    log; keep it small for quick probes.

    Returns a formatted block with: command, cwd, exit code, duration,
    timed-out flag, tail of stdout, tail of stderr.
    """
    timeout_seconds = max(1, min(int(timeout_seconds), 600))
    max_output_lines = max(1, min(int(max_output_lines), _OUTPUT_MAX_LINES_CAP))
    if not command or not command.strip():
        return "ERROR: command is empty."

    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or None,
        )
    except (OSError, FileNotFoundError) as e:
        return f"ERROR: failed to start command: {e}"

    timed_out = False
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

    duration_ms = int((time.monotonic() - start) * 1000)
    stdout = (stdout_b or b"").decode("utf-8", errors="replace")
    stderr = (stderr_b or b"").decode("utf-8", errors="replace")

    return _format_shell_result(
        command=command,
        cwd=cwd,
        exit_code=proc.returncode,
        duration_ms=duration_ms,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
        max_output_lines=max_output_lines,
    )


# The background-process registry — a session-scoped lookup table for
# long-running shell commands the agent has spawned (typically dev servers).
#
# There are two distinct "buffers" involved per background process:
#   1. The OS pipe between child and us. Fixed size (~64KB). If we never
#      drain it, the child's next write blocks and the dev server freezes.
#   2. Our own ring buffer (a `deque` with maxlen), where each drained line
#      is stored so `read_background_process_output` can return the last N lines.
#      `deque(maxlen=N)` silently drops the oldest entry when full, so
#      memory stays bounded for a process that runs for hours.

_BG_LOG_MAX_LINES = 5000


@dataclass
class _BackgroundProcess:
    handle: str
    session_id: str
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


_background: dict[str, _BackgroundProcess] = {}


async def _drain_background_output(bg: _BackgroundProcess) -> None:
    """Background task that empties the child's OS pipe one line at a time
    and appends each line to our ring buffer. Must run for the lifetime of
    the child — without it, the OS pipe fills and the child blocks on its
    next print/log. One drainer task per background process."""
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


@function_tool
async def start_background_process(
    command: str,
    cwd: str = "",
    name: str = "",
) -> str:
    """Spawn a long-running shell command in the background and return a handle.

    Use for dev servers and anything that won't exit on its own
    (`pnpm dev`, `next dev`, `uvicorn ... --reload`, etc.). For one-shot
    blocking commands use `run_shell` instead.

    The process inherits this container's env and network namespace. Its
    combined stdout+stderr is captured in-memory (ring buffer of ~5000 lines).
    Use `read_background_process_output(handle, tail_lines)` to read what it printed.

    The process is scoped to the current session — handles from other
    sessions cannot be read or stopped from here.

    Returns a handle string plus the pid and command for confirmation.
    """
    try:
        session_id = current_session_id.get()
    except LookupError:
        return "[start_background_process unavailable: no active session context]"

    if not command or not command.strip():
        return "ERROR: command is empty."

    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd or None,
        )
    except (OSError, FileNotFoundError) as e:
        return f"ERROR: failed to start command: {e}"

    handle = str(uuid.uuid4())
    bg = _BackgroundProcess(
        handle=handle,
        session_id=session_id,
        command=command,
        cwd=cwd,
        name=name or command[:40],
        pid=proc.pid,
        proc=proc,
        output=deque(maxlen=_BG_LOG_MAX_LINES),
        started_at=time.monotonic(),
    )
    bg.reader_task = asyncio.create_task(_drain_background_output(bg))
    _background[handle] = bg

    return (
        f"Started background process.\n"
        f"handle: {handle}\n"
        f"pid: {proc.pid}\n"
        f"command: {command}\n"
        f"cwd: {cwd or '<default>'}\n"
        f"Use read_background_process_output(handle) to see what it prints."
    )


@function_tool
async def read_background_process_output(handle: str, tail_lines: int = 200) -> str:
    """Return the most recent output lines from a background process.

    `handle` is the value returned by `start_background_process`. `tail_lines` caps
    how many of the most-recent lines to return (default 200, hard-capped
    at 5000). Pick a small value for quick health checks, a larger one to
    inspect a crash log.

    The response includes process status: running (with pid) or exited
    (with exit code).
    """
    try:
        session_id = current_session_id.get()
    except LookupError:
        return "[read_background_process_output unavailable: no active session context]"

    bg = _background.get(handle)
    if bg is None:
        return f"ERROR: no background process with handle={handle!r}."
    if bg.session_id != session_id:
        return f"ERROR: handle {handle!r} does not belong to this session."

    tail_lines = max(1, min(int(tail_lines), _BG_LOG_MAX_LINES))
    lines = list(bg.output)[-tail_lines:]

    if bg.exit_code is not None:
        status = f"exited (code={bg.exit_code})"
    elif bg.proc.returncode is None:
        status = f"running (pid={bg.pid})"
    else:
        status = f"exited (code={bg.proc.returncode})"

    body = "\n".join(lines) if lines else "<no output yet>"
    return (
        f"handle: {handle}\n"
        f"command: {bg.command}\n"
        f"cwd: {bg.cwd or '<default>'}\n"
        f"status: {status}\n"
        f"--- output (last {len(lines)} lines) ---\n{body}"
    )


@function_tool
async def stop_background_process(handle: str, grace_seconds: int = 5) -> str:
    """Stop a background process started with `start_background_process`.

    Sends SIGTERM first to let the process clean up (close ports, flush logs).
    If it hasn't exited after `grace_seconds` (default 5, capped at 60),
    sends SIGKILL.

    Call this when you're done verifying — leaks of e.g. `pnpm dev` will
    keep ports bound and hold container memory until the container restarts.

    The handle stays in the registry so you can still call
    `read_background_process_output` to inspect the final output afterwards.
    Returns the exit code and the last 20 lines of output.
    """
    try:
        session_id = current_session_id.get()
    except LookupError:
        return "[stop_background_process unavailable: no active session context]"

    bg = _background.get(handle)
    if bg is None:
        return f"ERROR: no background process with handle={handle!r}."
    if bg.session_id != session_id:
        return f"ERROR: handle {handle!r} does not belong to this session."

    grace_seconds = max(0, min(int(grace_seconds), 60))

    if bg.proc.returncode is not None:
        tail = list(bg.output)[-20:]
        return (
            f"handle: {handle}\n"
            f"status: already exited (code={bg.proc.returncode})\n"
            f"--- last {len(tail)} lines ---\n" + ("\n".join(tail) or "<no output>")
        )

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
    return (
        f"handle: {handle}\n"
        f"command: {bg.command}\n"
        f"stopped via: {signal_used}\n"
        f"exit_code: {bg.proc.returncode}\n"
        f"--- last {len(tail)} lines ---\n" + ("\n".join(tail) or "<no output>")
    )