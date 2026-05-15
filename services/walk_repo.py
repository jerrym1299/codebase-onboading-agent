import os

SKIP_DIRS = {
    "__pycache__", ".git",
    "node_modules", "dist", "build", "out", "coverage",
    ".next", ".turbo", ".nuxt", ".svelte-kit", ".vercel", ".cache",
}
CODE_EXTS = (
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".html", ".css", ".scss",
    ".json", ".yaml", ".yml",
    ".md", ".txt", ".csv",
    ".xls", ".xlsx", ".ppt", ".pptx",
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


def _walk(repo_dir: str):
    for dirpath, dirnames, filenames in os.walk(repo_dir):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        filenames[:] = [
            f for f in filenames
            if f.endswith(CODE_EXTS) or _is_always_included(f)
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
