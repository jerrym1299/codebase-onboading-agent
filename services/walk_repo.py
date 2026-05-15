import os

SKIP_DIRS = {
    "__pycache__", ".git",
    "node_modules", "dist", "build", "out", "coverage",
    ".next", ".turbo", ".nuxt", ".svelte-kit", ".vercel", ".cache",
}
SKIP_FILES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    "poetry.lock",
    "Pipfile.lock",
    "Gemfile.lock",
    "Cargo.lock",
    "go.sum",
}
SKIP_FILES_LOWER = {name.lower() for name in SKIP_FILES}
SKIP_SUFFIXES = (
    ".map",
    ".min.js",
    ".min.css",
    ".bundle.js",
    ".bundle.css",
    ".generated.js",
    ".generated.ts",
    ".generated.tsx",
)
MAX_INDEXED_FILE_BYTES = int(os.environ.get("CODE_INDEXING_MAX_FILE_BYTES", "1000000"))
CODE_EXTS = (
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".html", ".css", ".scss",
    ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf",
    ".env", ".env.example", ".env.sample", ".env.template",
    ".md", ".txt", ".csv",
)

# Files without a recognized extension (or with no extension at all) that should
# still be indexed. Critical for "how do I run this?" answers — env templates,
# Dockerfiles, Procfiles, runtime-version files, etc. NEVER include bare `.env`
# / `.env.local` / `.env.production` here: those routinely contain real secrets.
ALWAYS_INCLUDE_NAMES = frozenset({
    "Dockerfile", "Procfile", "Makefile", "makefile", "justfile",
    ".nvmrc", ".python-version", ".ruby-version", ".tool-versions",
    ".env.example", ".env.sample", ".env.template", ".env.dist",
    "env.example", "env.sample", "env.template", "env.dist",
})


def _is_always_included(filename: str) -> bool:
    if filename in ALWAYS_INCLUDE_NAMES:
        return True
    if filename.startswith("Dockerfile."):
        return True
    return False


def _should_index_file(dirpath: str, filename: str) -> bool:
    lower = filename.lower()
    if filename in SKIP_FILES or lower in SKIP_FILES_LOWER:
        return False
    if lower.endswith(SKIP_SUFFIXES):
        return False
    if not lower.endswith(CODE_EXTS):
        return False
    try:
        if os.path.getsize(os.path.join(dirpath, filename)) > MAX_INDEXED_FILE_BYTES:
            return False
    except OSError:
        return False
    return True


def _walk(repo_dir: str):
    for dirpath, dirnames, filenames in os.walk(repo_dir):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        filenames[:] = [
            f for f in filenames
            if _is_always_included(f) or _should_index_file(dirpath, f)
        ]
        yield dirpath, dirnames, filenames


async def walk_repo(repo_dir: str) -> str:
    lines = []
    for dirpath, _, filenames in _walk(repo_dir):
        indent = "  " * dirpath.replace(repo_dir, "").count(os.sep)
        lines.append(f"{indent}{os.path.basename(dirpath)}/")
        lines.extend(f"{indent}{f}" for f in filenames)
    return "\n".join(lines)


async def collect_file_paths(repo_dir: str) -> list[str]:
    """Walk the repo and return a flat list of absolute file paths."""
    return [
        os.path.join(dirpath, f)
        for dirpath, _, filenames in _walk(repo_dir)
        for f in filenames
    ]
