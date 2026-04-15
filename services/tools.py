from pathlib import Path
import re
import subprocess
from agents import function_tool
from services.chunk_and_embed import embed_query
from services.db import get_pool
SEARCH_SQL = """
    SELECT file_path, chunk_type, name, start_line, end_line, content,
           1 - (embedding <=> %s::vector) AS similarity
    FROM code_chunks
    WHERE repo_url = %s
    ORDER BY embedding <=> %s::vector
    LIMIT %s
"""

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
    with open(file_path, "r", errors="replace") as f:
        content = f.read()

    ext = Path(file_path).suffix.lower()
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
        await cur.execute(SEARCH_SQL, (emb, repo_url, emb, k))
        rows = await cur.fetchall()
    results = []
    for r in rows:
        results.append(
            f"[{r[6]:.3f}] {r[0]} ({r[1]}: {r[2]}) L{r[3]}-{r[4]}\n{r[5][:500]}"
        )
    return "\n---\n".join(results) if results else "No matching chunks found."

@function_tool 
def git_log(path:str, limit:int = 10) -> list[str]:
    # get the git log recent commits + messages
    try:
        result = subprocess.run(["git", "log", "--pretty=format:%h %s", "-n", str(limit)], cwd=path, capture_output=True, text=True)
        return result.stdout.splitlines()
    except (subprocess.CalledProcessError, OSError):
        return []
