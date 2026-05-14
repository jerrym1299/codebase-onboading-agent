import contextvars
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

from agents import function_tool
from temporalio.client import Client

from services.chunk_and_embed import embed_query
from services.db import (
    CODE_SEARCH_SQL, DIR_SUMMARY_SEARCH_SQL, get_pool, get_startup_plan_row,
    search_repo_text_lines,
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
async def search_exact_indexed(
    query: str,
    repo_url: str,
    limit: int = 50,
    regex: bool = False,
    path: str = "",
    language: str = "",
) -> str:
    """Search the indexed line inventory for exact strings or regexes."""
    try:
        rows = await search_repo_text_lines(
            repo_url.rstrip("/"),
            query,
            regex=regex,
            path=path,
            language=language,
            limit=limit,
        )
    except ValueError as exc:
        return f"ERROR: {exc}"

    if not rows:
        return "No exact matches found."
    return "\n".join(
        f"{row['file_path']}:{row['line_number']}: {row['line_text']}"
        for row in rows
    )


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
