"""
LLM-first startup analysis: build a context bundle of key repo files,
call OpenAI with a JSON-schema constrained response, and persist the
resulting plan.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI

# Per-bucket character budgets. The total bundle is capped at ~32k chars;
# if total exceeds the budget, drop buckets in reverse priority order.
TOTAL_BUDGET_CHARS = 128_000

# Files that must always be included (priority 0 — never dropped before
# everything else). Split into two collectors: root-only canonical files
# (README, lockfiles, workspace declarations) and recursive files that
# legitimately vary per package in a monorepo (env templates, per-package
# manifests, runtime-version pins). Env templates MUST be recursive: a repo
# like hobbesBackend has the OAuth client vars in `backend/env.example`,
# not at the root, and a root-only scan silently drops those vars from
# the resulting plan.
ALWAYS_FILES_ROOT_ONLY = (
    "README.md", "README.MD", "README", "README.rst", "README.txt",
    "pnpm-workspace.yaml", "pnpm-workspace.yml",
    "turbo.json", "nx.json", "lerna.json",
)
ALWAYS_FILES_RECURSIVE = (
    "package.json",
    "pyproject.toml", "Pipfile", "setup.py", "setup.cfg",
    "poetry.lock", "uv.lock",
    "go.mod", "Cargo.toml",
    "Gemfile", "composer.json",
    "pom.xml", "build.gradle", "build.gradle.kts",
    ".env.example", ".env.sample", ".env.template", ".env.dist",
    "env.example", "env.sample", "env.template", "env.dist",
    ".nvmrc", ".python-version", ".ruby-version", ".tool-versions",
)
ALWAYS_GLOBS_RECURSIVE = ("requirements*.txt",)

LOCKFILE_NAMES = frozenset({"poetry.lock", "uv.lock", "Cargo.toml", "Gemfile"})

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
    bucket: str           # 'always' | 'env_refs' | 'infra' | 'ci' | 'migrations' | 'skeleton'
    path: str             # repo-relative
    content: str          # text payload included in the prompt


@dataclass
class ContextBundle:
    entries: list[BundleEntry]
    truncations: list[str]   # bucket names that were dropped to fit the budget
    total_chars: int


@dataclass
class EnvRef:
    name: str
    file: str
    line: int
    snippet: str
    hard: bool   # True if reference would raise/abort when the var is unset


# Patterns are scoped by file kind so we don't false-match Python syntax in JS
# files and vice versa. The capture group is always the env var name.
_PY_PATTERNS: tuple[tuple[re.Pattern[str], bool], ...] = (
    (re.compile(r"""os\.environ\s*\[\s*["']([A-Z][A-Z0-9_]*)["']\s*\]"""), True),
    # os.environ.get(...) without a default (no comma before close paren) is soft
    # (returns None) but easy to forget — keep as soft. With a default → soft.
    (re.compile(r"""os\.environ\.get\(\s*["']([A-Z][A-Z0-9_]*)["']"""), False),
    (re.compile(r"""os\.getenv\(\s*["']([A-Z][A-Z0-9_]*)["']"""), False),
)

_JS_PATTERNS: tuple[tuple[re.Pattern[str], bool], ...] = (
    (re.compile(r"""process\.env\.([A-Z][A-Z0-9_]+)"""), False),
    (re.compile(r"""process\.env\[\s*["']([A-Z][A-Z0-9_]+)["']\s*\]"""), False),
    (re.compile(r"""import\.meta\.env\.([A-Z][A-Z0-9_]+)"""), False),
)

_SHELL_YAML_PATTERNS: tuple[tuple[re.Pattern[str], bool], ...] = (
    # ${VAR:?msg} — POSIX hard-fail-if-unset
    (re.compile(r"""\$\{([A-Z][A-Z0-9_]+):\?[^}]*\}"""), True),
    # ${VAR} or ${VAR:-default}
    (re.compile(r"""\$\{([A-Z][A-Z0-9_]+)(?::[^}]*)?\}"""), False),
)

_ENV_FILE_PATTERNS: tuple[tuple[re.Pattern[str], bool], ...] = (
    (re.compile(r"""^\s*([A-Z][A-Z0-9_]+)\s*="""), False),
)

_ENV_TEMPLATE_NAMES = frozenset({
    ".env.example", ".env.sample", ".env.template", ".env.dist",
    "env.example", "env.sample", "env.template", "env.dist",
})

_SCAN_SKIP_DIRS = frozenset({
    "node_modules", ".git", ".next", "dist", "build", "out",
    "venv", ".venv", "__pycache__", ".pytest_cache",
    "target", "vendor", ".turbo", ".cache", ".yarn", ".pnpm-store",
})

_SHELL_NOISE_VARS = frozenset({
    "PATH", "HOME", "USER", "SHELL", "PWD", "OLDPWD", "PS1", "TERM",
    "LANG", "LC_ALL", "TZ", "TMPDIR", "EDITOR", "VISUAL",
    "BASH_VERSION", "OSTYPE", "HOSTNAME", "UID", "GID", "EUID",
    "IFS", "RANDOM", "LINENO", "SECONDS",
})


def _classify_file(path: Path) -> tuple[re.Pattern[str], ...] | None:
    """Return the pattern set for this file, or None to skip."""
    ext = path.suffix.lower()
    name_lower = path.name.lower()
    if ext == ".py":
        return _PY_PATTERNS
    if ext in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
        return _JS_PATTERNS
    if ext in {".sh", ".bash", ".zsh", ".yml", ".yaml"} \
            or "dockerfile" in name_lower or "compose" in name_lower \
            or name_lower in {"procfile", "makefile"}:
        return _SHELL_YAML_PATTERNS
    # Only scan template/example env files — never bare `.env`,
    # `.env.local`, `.env.production`, etc., which routinely contain real
    # secrets.
    if name_lower in _ENV_TEMPLATE_NAMES:
        return _ENV_FILE_PATTERNS
    return None


def scan_env_refs(
    repo_dir: Path,
    *,
    max_files: int = 4000,
    max_lines_per_file: int = 5000,
) -> list[EnvRef]:
    """Walk source files and aggregate env var references with hard-fail
    classification. Hard-fail = reference would raise/abort if the var is
    unset (e.g. os.environ["X"], ${X:?msg})."""
    refs: list[EnvRef] = []
    files_seen = 0
    for path in repo_dir.rglob("*"):
        if files_seen >= max_files:
            break
        if any(part in _SCAN_SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        patterns = _classify_file(path)
        if patterns is None:
            continue
        files_seen += 1
        try:
            with path.open("r", errors="replace") as f:
                lines = [next(f, None) for _ in range(max_lines_per_file)]
                lines = [ln for ln in lines if ln is not None]
        except (OSError, UnicodeDecodeError):
            continue
        rel = str(path.relative_to(repo_dir))
        is_env_file = patterns is _ENV_FILE_PATTERNS
        for ln_idx, line in enumerate(lines, start=1):
            for pattern, hard in patterns:
                for m in pattern.finditer(line):
                    name = m.group(1)
                    if name in _SHELL_NOISE_VARS:
                        continue
                    if is_env_file:
                        snippet = f"{name}=…"
                    else:
                        snippet = line.rstrip()[:220]
                    refs.append(EnvRef(
                        name=name,
                        file=rel,
                        line=ln_idx,
                        snippet=snippet,
                        hard=hard,
                    ))
    return refs


def render_env_refs(refs: list[EnvRef], *, max_refs_per_var: int = 8) -> str:
    """Group refs by var and render a structured block for the LLM. Marks
    each var HARD-FAIL if any reference is hard, otherwise soft."""
    if not refs:
        return "(no env var references found in source)"
    by_var: dict[str, list[EnvRef]] = defaultdict(list)
    for r in refs:
        by_var[r.name].append(r)
    lines: list[str] = [
        "# Env var references discovered in source code",
        "# Each entry is the ground truth for what the code actually reads",
        "# from the environment. Use this — not the README — as the canonical",
        "# list of env vars to include in the plan.",
        "",
    ]
    for name in sorted(by_var.keys()):
        group = by_var[name]
        hard = any(r.hard for r in group)
        lines.append(
            f"## {name} ({'HARD-FAIL' if hard else 'soft'}) — "
            f"{len(group)} reference(s)"
        )
        for r in group[:max_refs_per_var]:
            marker = "!" if r.hard else "-"
            lines.append(f"  {marker} {r.file}:{r.line}: {r.snippet.strip()}")
        if len(group) > max_refs_per_var:
            lines.append(f"  … +{len(group) - max_refs_per_var} more reference(s)")
        lines.append("")
    return "\n".join(lines).rstrip()


def _read_text(path: Path, max_lines: int | None = None) -> str | None:
    try:
        with path.open("r", errors="replace") as f:
            if max_lines is None:
                return f.read()
            return "".join(f.readline() for _ in range(max_lines))
    except (OSError, UnicodeDecodeError):
        return None


_PER_BUCKET_FILE_CAP = 60


def _collect_root_only(repo_dir: Path, names: tuple[str, ...]) -> list[Path]:
    """Pick up canonical-at-root files (README, lockfile, workspace decl).
    Multiple READMEs across a monorepo are noise, not signal — only the root
    one belongs in the bundle."""
    out: list[Path] = []
    for name in names:
        candidate = repo_dir / name
        if candidate.is_file():
            out.append(candidate)
    return sorted(set(out))


def _collect_recursive(repo_dir: Path, names: tuple[str, ...],
                       globs: tuple[str, ...]) -> list[Path]:
    """Recursively find files matching the given basenames or glob patterns,
    pruning skip dirs. Use for files that legitimately vary per package in a
    monorepo (env templates, per-package manifests, Dockerfile variants).
    """
    name_set = {n.lower() for n in names}
    out: set[Path] = set()
    if name_set:
        for path in repo_dir.rglob("*"):
            if any(part in _SCAN_SKIP_DIRS for part in path.parts):
                continue
            if not path.is_file():
                continue
            if path.name.lower() in name_set:
                out.add(path)
    for pattern in globs:
        for p in repo_dir.rglob(pattern):
            if any(part in _SCAN_SKIP_DIRS for part in p.parts):
                continue
            if p.is_file():
                out.add(p)
    return sorted(out)[:_PER_BUCKET_FILE_CAP]


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
    always_paths = (
        _collect_root_only(root, ALWAYS_FILES_ROOT_ONLY)
        + _collect_recursive(root, ALWAYS_FILES_RECURSIVE, ALWAYS_GLOBS_RECURSIVE)
    )
    seen: set[Path] = set()
    for path in always_paths:
        if path in seen:
            continue
        seen.add(path)
        max_lines = 40 if path.name in LOCKFILE_NAMES else None
        text = _read_text(path, max_lines=max_lines)
        if text is not None:
            always_entries.append(BundleEntry(
                bucket="always",
                path=str(path.relative_to(root)),
                content=text,
            ))
    buckets.append(("always", always_entries))

    # Priority 2: env var references discovered in source. Ground truth for
    # which env vars the code actually reads — used by the LLM to enumerate
    # required/optional vars regardless of whether the README mentions them.
    env_refs = scan_env_refs(root)
    env_refs_entries: list[BundleEntry] = []
    if env_refs:
        env_refs_entries.append(BundleEntry(
            bucket="env_refs",
            path="<env_var_references>",
            content=render_env_refs(env_refs),
        ))
    buckets.append(("env_refs", env_refs_entries))

    # Priority 3: infra
    infra_entries: list[BundleEntry] = []
    for path in _collect_recursive(root, INFRA_FILES, INFRA_GLOBS):
        text = _read_text(path)
        if text is not None:
            infra_entries.append(BundleEntry(
                bucket="infra", path=str(path.relative_to(root)), content=text,
            ))
    buckets.append(("infra", infra_entries))

    # Priority 4: ci (head 200 lines each)
    ci_entries: list[BundleEntry] = []
    for path in _collect_recursive(root, CI_FILES, CI_GLOBS):
        text = _read_text(path, max_lines=200)
        if text is not None:
            ci_entries.append(BundleEntry(
                bucket="ci", path=str(path.relative_to(root)), content=text,
            ))
    buckets.append(("ci", ci_entries))

    # Priority 5: migrations
    mig_entries: list[BundleEntry] = []
    for path in _collect_recursive(root, MIGRATION_FILES, MIGRATION_GLOBS):
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


# JSON schema enforced via OpenAI response_format. Strict mode requires every
# property to be listed in `required`; nullability is expressed via `["type", "null"]`.
PLAN_JSON_SCHEMA: dict = {
    "name": "startup_plan",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "summary", "is_monorepo", "packages", "warnings"],
        "properties": {
            "schema_version": {"type": "string"},
            "summary": {"type": "string"},
            "is_monorepo": {"type": "boolean"},
            "packages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "path", "name", "framework", "runtime", "package_manager",
                        "external_tools", "services", "env_vars", "steps",
                    ],
                    "properties": {
                        "path": {"type": "string"},
                        "name": {"type": ["string", "null"]},
                        "framework": {"type": ["string", "null"]},
                        "runtime": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["language", "version", "version_source", "confidence"],
                            "properties": {
                                "language": {"type": "string"},
                                "version": {"type": ["string", "null"]},
                                "version_source": {"type": ["string", "null"]},
                                "confidence": {"type": "number"},
                            },
                        },
                        "package_manager": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["name", "version", "source", "confidence"],
                            "properties": {
                                "name": {"type": "string"},
                                "version": {"type": ["string", "null"]},
                                "source": {"type": ["string", "null"]},
                                "confidence": {"type": "number"},
                            },
                        },
                        "external_tools": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["name", "required", "reason", "confidence"],
                                "properties": {
                                    "name": {"type": "string"},
                                    "required": {"type": "boolean"},
                                    "reason": {"type": "string"},
                                    "confidence": {"type": "number"},
                                },
                            },
                        },
                        "services": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["name", "image", "source", "confidence"],
                                "properties": {
                                    "name": {"type": "string"},
                                    "image": {"type": ["string", "null"]},
                                    "source": {"type": ["string", "null"]},
                                    "confidence": {"type": "number"},
                                },
                            },
                        },
                        "env_vars": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "name", "required", "example", "sources",
                                    "confidence", "needs_verification",
                                ],
                                "properties": {
                                    "name": {"type": "string"},
                                    "required": {"type": "boolean"},
                                    "example": {"type": ["string", "null"]},
                                    "sources": {"type": "array", "items": {"type": "string"}},
                                    "confidence": {"type": "number"},
                                    "needs_verification": {"type": "boolean"},
                                },
                            },
                        },
                        "steps": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "order", "title", "command", "cwd", "explain",
                                    "confidence", "needs_verification",
                                ],
                                "properties": {
                                    "order": {"type": "integer"},
                                    "title": {"type": "string"},
                                    "command": {"type": "string"},
                                    "cwd": {"type": "string"},
                                    "explain": {"type": "string"},
                                    "confidence": {"type": "number"},
                                    "needs_verification": {"type": "boolean"},
                                },
                            },
                        },
                    },
                },
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    },
    "strict": True,
}


SYSTEM_PROMPT = (
    "You are a senior software engineer specializing in repository onboarding. "
    "Given a curated bundle of files from a freshly cloned codebase, produce a "
    "structured 'how to run this locally' plan. Be precise, cite real file paths "
    "in `sources`/`source`/`version_source`, and assign honest `confidence` (0..1). "
    "If you have to guess a value, set `needs_verification: true`. Do not invent "
    "files, scripts, or commands that aren't supported by the bundle. If a piece "
    "of information is genuinely unknown, omit it (use null where the schema "
    "allows) and add a brief note to `warnings`. "
    "\n\n"
    "ENV VARS — highest-stakes part of the plan:\n"
    "1. The bundle contains a block titled `# Env var references discovered in "
    "source code`. Treat this as GROUND TRUTH for which env vars exist. Every "
    "var listed there MUST appear in the relevant package's `env_vars` array.\n"
    "2. README and prose docs are unreliable for env vars — they routinely "
    "under-document. WEIGHT THEM LESS than the env_refs block and .env.example/"
    ".env.template files. If a var appears in env_refs but not in README/"
    ".env.example, INCLUDE IT ANYWAY and set `needs_verification: true`.\n"
    "3. Mark a var `required: true` when ANY of its references is HARD-FAIL "
    "(marked with `HARD-FAIL` in the env_refs block, or with a `!` prefix on a "
    "specific line). HARD-FAIL means the code raises / aborts when the var is "
    "unset (`os.environ[\"X\"]`, `${X:?msg}`, throw-on-missing patterns). "
    "Otherwise `required: false`.\n"
    "4. For each env var, populate `sources` with EVERY file:line where it is "
    "referenced (from env_refs) plus any env template files (.env.example etc.) "
    "that mention it. Do not invent sources.\n"
    "5. If you cannot determine an `example` value from .env.example, code "
    "defaults, or docker-compose, set `example: null` and `needs_verification: "
    "true`. Do not fabricate plausible-looking values.\n"
    "\n"
    "Include everything required to get the user to the core functionality of "
    "the application."
)

ANALYSIS_MODEL = os.environ.get("STARTUP_ANALYSIS_MODEL", "gpt-5.4")


_openai_client: OpenAI | None = None


def _client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI()
    return _openai_client


@dataclass
class AnalysisResult:
    plan: dict
    raw_response: str
    prompt_tokens: int
    completion_tokens: int


def call_llm(bundle: ContextBundle) -> AnalysisResult:
    """Call OpenAI with the response_format JSON schema. Raises on parse failure."""
    rendered = render_bundle(bundle)
    response = _client().chat.completions.create(
        model=ANALYSIS_MODEL,
        temperature=0.1,
        response_format={"type": "json_schema", "json_schema": PLAN_JSON_SCHEMA},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": rendered},
        ],
    )
    raw = response.choices[0].message.content or ""
    plan = json.loads(raw)
    usage = response.usage
    return AnalysisResult(
        plan=plan,
        raw_response=raw,
        prompt_tokens=getattr(usage, "prompt_tokens", 0),
        completion_tokens=getattr(usage, "completion_tokens", 0),
    )


