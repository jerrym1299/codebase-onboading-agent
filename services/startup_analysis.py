"""
LLM-first startup analysis: build a context bundle of key repo files,
call OpenAI with a JSON-schema constrained response, validate commands
against the cloned repo, and persist the resulting plan.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Per-bucket character budgets. The total bundle is capped at ~32k chars;
# if total exceeds the budget, drop buckets in reverse priority order.
TOTAL_BUDGET_CHARS = 32_000

# Files that must always be included (priority 0 — never dropped before
# everything else).
ALWAYS_FILES = (
    "README.md", "README.MD", "README", "README.rst", "README.txt",
    "package.json",
    "pnpm-workspace.yaml", "pnpm-workspace.yml",
    "turbo.json", "nx.json", "lerna.json",
    "pyproject.toml", "Pipfile", "setup.py", "setup.cfg",
    "poetry.lock", "uv.lock",
    "go.mod", "Cargo.toml",
    "Gemfile", "composer.json",
    "pom.xml", "build.gradle", "build.gradle.kts",
)
ALWAYS_GLOBS = ("requirements*.txt",)

ENV_FILES = (".env.example", ".env.sample", ".env.template", ".env.dist")

INFRA_FILES = (
    "Dockerfile", "Procfile", "Makefile", "justfile",
    ".tool-versions", ".nvmrc", ".python-version", ".ruby-version",
)
INFRA_GLOBS = ("Dockerfile.*", "docker-compose*.yml", "docker-compose*.yaml",
               "compose*.yml", "compose*.yaml")

CI_FILES = (
    ".gitlab-ci.yml",
    "vercel.json", "vercel.ts", "netlify.toml",
    "railway.toml", "fly.toml", "wrangler.toml",
)
CI_GLOBS = (".github/workflows/*.yml", ".github/workflows/*.yaml")

MIGRATION_FILES = ("prisma/schema.prisma", "alembic.ini")
MIGRATION_GLOBS = ("knexfile*",)


@dataclass
class BundleEntry:
    """One file (or directory listing) included in the context bundle."""
    bucket: str           # 'always' | 'env' | 'infra' | 'ci' | 'migrations' | 'skeleton'
    path: str             # repo-relative
    content: str          # text payload included in the prompt


@dataclass
class ContextBundle:
    entries: list[BundleEntry]
    truncations: list[str]   # bucket names that were dropped to fit the budget
    total_chars: int


def _read_text(path: Path, max_lines: int | None = None) -> str | None:
    try:
        with path.open("r", errors="replace") as f:
            if max_lines is None:
                return f.read()
            return "".join(f.readline() for _ in range(max_lines))
    except (OSError, UnicodeDecodeError):
        return None


def _collect_matches(repo_dir: Path, names: tuple[str, ...],
                     globs: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for name in names:
        candidate = repo_dir / name
        if candidate.is_file():
            out.append(candidate)
    for pattern in globs:
        out.extend(p for p in repo_dir.glob(pattern) if p.is_file())
    return sorted(set(out))


def _top_two_levels(repo_dir: Path) -> str:
    """Return a compact text listing of files in the top two directory levels."""
    lines: list[str] = []
    for entry in sorted(repo_dir.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        rel = entry.relative_to(repo_dir)
        if entry.is_dir():
            lines.append(f"{rel}/")
            for child in sorted(entry.iterdir(), key=lambda p: p.name)[:80]:
                if child.name.startswith("."):
                    continue
                lines.append(f"  {child.relative_to(repo_dir)}{'/' if child.is_dir() else ''}")
        else:
            lines.append(str(rel))
    return "\n".join(lines)


def build_context(repo_dir: str) -> ContextBundle:
    """Walk the cloned repo and collect a fixed-priority bundle of files.
    Drops low-priority buckets until total size fits TOTAL_BUDGET_CHARS."""
    root = Path(repo_dir)
    if not root.is_dir():
        raise ValueError(f"repo_dir does not exist or is not a directory: {repo_dir}")

    buckets: list[tuple[str, list[BundleEntry]]] = []

    # Priority 1 (highest): always
    always_entries: list[BundleEntry] = []
    for path in _collect_matches(root, ALWAYS_FILES, ALWAYS_GLOBS):
        text = _read_text(path)
        if text is not None:
            always_entries.append(BundleEntry(
                bucket="always",
                path=str(path.relative_to(root)),
                content=text,
            ))
    buckets.append(("always", always_entries))

    # Priority 2: env hints
    env_entries: list[BundleEntry] = []
    for path in _collect_matches(root, ENV_FILES, ()):
        text = _read_text(path)
        if text is not None:
            env_entries.append(BundleEntry(
                bucket="env", path=str(path.relative_to(root)), content=text,
            ))
    buckets.append(("env", env_entries))

    # Priority 3: infra
    infra_entries: list[BundleEntry] = []
    for path in _collect_matches(root, INFRA_FILES, INFRA_GLOBS):
        text = _read_text(path)
        if text is not None:
            infra_entries.append(BundleEntry(
                bucket="infra", path=str(path.relative_to(root)), content=text,
            ))
    buckets.append(("infra", infra_entries))

    # Priority 4: ci (head 200 lines each)
    ci_entries: list[BundleEntry] = []
    for path in _collect_matches(root, CI_FILES, CI_GLOBS):
        text = _read_text(path, max_lines=200)
        if text is not None:
            ci_entries.append(BundleEntry(
                bucket="ci", path=str(path.relative_to(root)), content=text,
            ))
    buckets.append(("ci", ci_entries))

    # Priority 5: migrations
    mig_entries: list[BundleEntry] = []
    for path in _collect_matches(root, MIGRATION_FILES, MIGRATION_GLOBS):
        text = _read_text(path)
        if text is not None:
            mig_entries.append(BundleEntry(
                bucket="migrations", path=str(path.relative_to(root)), content=text,
            ))
    mig_dir = root / "migrations"
    if mig_dir.is_dir():
        listing = "\n".join(sorted(p.name for p in mig_dir.iterdir()))
        mig_entries.append(BundleEntry(
            bucket="migrations", path="migrations/", content=listing,
        ))
    buckets.append(("migrations", mig_entries))

    # Priority 6 (lowest): skeleton
    buckets.append(("skeleton", [BundleEntry(
        bucket="skeleton", path="<top-2-levels>", content=_top_two_levels(root),
    )]))

    # Drop buckets in reverse priority order until under budget.
    truncations: list[str] = []
    while True:
        total = sum(len(e.content) for _, entries in buckets for e in entries)
        if total <= TOTAL_BUDGET_CHARS:
            break
        # Drop the lowest-priority non-empty bucket.
        for i in range(len(buckets) - 1, -1, -1):
            name, entries = buckets[i]
            if entries:
                truncations.append(name)
                buckets[i] = (name, [])
                break
        else:
            break  # everything empty; nothing more to drop

    flat: list[BundleEntry] = [e for _, entries in buckets for e in entries]
    return ContextBundle(
        entries=flat,
        truncations=truncations,
        total_chars=sum(len(e.content) for e in flat),
    )


def render_bundle(bundle: ContextBundle) -> str:
    """Convert a bundle into the developer-message string fed to the LLM."""
    blocks: list[str] = []
    for entry in bundle.entries:
        blocks.append(f"--- {entry.path} ({entry.bucket}) ---\n{entry.content}")
    if bundle.truncations:
        blocks.append(
            f"--- truncations ---\nDropped buckets due to size: "
            f"{', '.join(bundle.truncations)}"
        )
    return "\n\n".join(blocks)
