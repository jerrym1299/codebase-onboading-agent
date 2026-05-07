# Startup Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "how to run this repo locally" feature: an LLM-first analysis activity that produces a structured startup plan during indexing, surfaced via a new `bootstrap_agent` and two REST endpoints, recomputable on demand.

**Architecture:** A new Temporal activity (`analyze_startup_activity`) runs sequentially after `index_repo_activity` and persists a `startup_plans` row keyed on `repo_url`. The activity builds a deterministic context bundle from key files, calls OpenAI with a JSON-schema-constrained `response_format`, validates commands against the cloned repo, and publishes a `data-startup-plan-updated` event to the per-session bus. A new `bootstrap_agent` reads the plan via `get_startup_plan` and can trigger recomputes through a workflow signal exposed both as an agent tool and as `POST /sessions/:id/startup-plan/recompute`.

**Tech Stack:** Python 3.x, FastAPI, Temporal, OpenAI SDK (`openai`), psycopg + pgvector, openai-agents SDK, async/await throughout.

**Spec:** [docs/superpowers/specs/2026-05-07-startup-analysis-design.md](../specs/2026-05-07-startup-analysis-design.md)

**Testing convention:** This project does NOT use pytest. Per CLAUDE.md, verification is curl/HTTP-based against a running FastAPI server (`docker compose up -d`, base URL `http://localhost:8000`). Each task ends with an inline Python or curl smoke check. A new manual script `scripts/test_startup_plan.py` (Task 11) is the equivalent of an integration test.

**Test repo:** `https://github.com/ThomasBenjaminCook/WattAppWebApp` per CLAUDE.md.

---

## Pre-flight (Task 0)

### Task 0: Verify the environment is running

**Files:** None.

- [ ] **Step 1: Confirm the stack is up**

```bash
docker compose ps
```

Expected: `fastapi`, `temporal`, `temporal-ui`, `postgres` services all show `running`. If not, run `docker compose up -d` and re-check.

- [ ] **Step 2: Smoke-check FastAPI**

```bash
curl -s http://localhost:8000/ | python3 -m json.tool
```

Expected: `{ "Hello": "world" }`.

- [ ] **Step 3: Confirm `OPENAI_API_KEY` is set in the FastAPI container**

```bash
docker compose exec fastapi printenv OPENAI_API_KEY | head -c 8 && echo "..."
```

Expected: prefix of the key (e.g. `sk-proj-...`). If empty, populate `.env` and `docker compose up -d --force-recreate fastapi`.

---

## Task 1: Add `startup_plans` table

**Files:**
- Modify: `services/db.py:59-138` (extend `SCHEMA_SQL`)
- Modify: `services/db.py` (add `get_startup_plan_row`, `upsert_startup_plan` helpers and `STARTUP_PLAN_SELECT_SQL`)

- [ ] **Step 1: Append the new table to `SCHEMA_SQL`**

Edit `services/db.py`. Add the following block to the end of the `SCHEMA_SQL` string (just before the closing `"""`):

```sql

CREATE TABLE IF NOT EXISTS startup_plans (
    repo_url           TEXT PRIMARY KEY,
    plan               JSONB NOT NULL,
    analysis_status    TEXT NOT NULL CHECK (analysis_status IN ('ok', 'partial', 'failed')),
    overall_confidence REAL,
    model              TEXT NOT NULL,
    truncations        TEXT[] NOT NULL DEFAULT '{}',
    error              TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

- [ ] **Step 2: Add helpers at the bottom of `services/db.py`**

Append after `store_dir_summaries`:

```python
STARTUP_PLAN_SELECT_SQL = """
    SELECT plan, analysis_status, overall_confidence, model, truncations, error,
           created_at, updated_at
    FROM startup_plans
    WHERE repo_url = %s
"""


async def get_startup_plan_row(repo_url: str) -> dict | None:
    """Return the startup plan row for a repo, or None if absent."""
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(STARTUP_PLAN_SELECT_SQL, (repo_url,))
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "plan": row[0],
        "analysis_status": row[1],
        "overall_confidence": row[2],
        "model": row[3],
        "truncations": list(row[4] or []),
        "error": row[5],
        "created_at": row[6].isoformat(),
        "updated_at": row[7].isoformat(),
    }


async def upsert_startup_plan(
    repo_url: str,
    plan: dict,
    analysis_status: str,
    overall_confidence: float | None,
    model: str,
    truncations: list[str],
    error: str | None,
) -> None:
    """Insert or replace the startup plan row for a repo."""
    sql = """
        INSERT INTO startup_plans
            (repo_url, plan, analysis_status, overall_confidence, model,
             truncations, error, updated_at)
        VALUES (%s, %s::jsonb, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (repo_url) DO UPDATE SET
            plan               = EXCLUDED.plan,
            analysis_status    = EXCLUDED.analysis_status,
            overall_confidence = EXCLUDED.overall_confidence,
            model              = EXCLUDED.model,
            truncations        = EXCLUDED.truncations,
            error              = EXCLUDED.error,
            updated_at         = NOW()
    """
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            sql,
            (
                repo_url,
                json.dumps(plan),
                analysis_status,
                overall_confidence,
                model,
                truncations,
                error,
            ),
        )
```

Add `import json` at the top of `services/db.py` (just below the existing `import os`).

- [ ] **Step 3: Restart the FastAPI container to apply the schema**

```bash
docker compose restart fastapi
```

The `init_schema()` lifespan hook re-runs and the `IF NOT EXISTS` guard creates the new table on first startup, leaves it alone on subsequent ones.

- [ ] **Step 4: Verify the table exists**

```bash
docker compose exec postgres psql -U postgres -d codebase_agent -c "\d startup_plans"
```

Expected: a table description showing `repo_url`, `plan jsonb`, `analysis_status text`, `overall_confidence real`, `model text`, `truncations text[]`, `error text`, `created_at timestamptz`, `updated_at timestamptz`.

- [ ] **Step 5: Verify the helpers import without errors**

```bash
docker compose exec fastapi python3 -c "from services.db import get_startup_plan_row, upsert_startup_plan, STARTUP_PLAN_SELECT_SQL; print('ok')"
```

Expected: `ok`.

- [ ] **Step 6: Commit**

```bash
git add services/db.py
git commit -m "$(cat <<'EOF'
feat(db): add startup_plans table + read/upsert helpers

Backs the new startup-analysis feature. One row per repo_url with the
structured plan JSON, analysis status, overall confidence, and the model
used. Helpers mirror the dataclass-free style used by store_chunks.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Context bundle builder

**Files:**
- Create: `services/startup_analysis.py`

This task introduces the deterministic file-collection logic. No LLM, no DB. Pure function so we can iterate cheaply.

- [ ] **Step 1: Create `services/startup_analysis.py` with the bundle builder**

```python
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
```

- [ ] **Step 2: Smoke-check the bundle builder against the test repo**

The test repo is already cloned by previous test runs at `/repos/WattAppWebApp` inside the FastAPI container.

```bash
docker compose exec fastapi python3 - <<'PY'
import asyncio
from services.clone_repo import ensure_repo_dir
from services.startup_analysis import build_context, render_bundle

async def main():
    repo_dir = await ensure_repo_dir("https://github.com/ThomasBenjaminCook/WattAppWebApp")
    bundle = build_context(repo_dir)
    print(f"entries={len(bundle.entries)} chars={bundle.total_chars} truncations={bundle.truncations}")
    for e in bundle.entries[:5]:
        print(f"  {e.bucket:>10} {e.path} ({len(e.content)} chars)")
    print("---rendered head---")
    print(render_bundle(bundle)[:600])

asyncio.run(main())
PY
```

Expected: prints non-zero `entries` (at least `package.json` and a `README` for that repo), `chars` under 32000, and a head of the rendered text starting with `--- package.json (always) ---` or similar.

- [ ] **Step 3: Commit**

```bash
git add services/startup_analysis.py
git commit -m "$(cat <<'EOF'
feat(startup-analysis): add context bundle builder

Deterministic file collection that groups manifests, env hints, infra
configs, CI yaml, migration files, and a top-2-level skeleton listing
into a budgeted bundle. Drops low-priority buckets first to stay under
the LLM token budget.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: JSON schema + LLM call + validator

**Files:**
- Modify: `services/startup_analysis.py` (append schema, LLM call, validator)

- [ ] **Step 1: Append the OpenAI response schema constant**

Add to the bottom of `services/startup_analysis.py`:

```python
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
    "allows) and add a brief note to `warnings`."
)

ANALYSIS_MODEL = os.environ.get("STARTUP_ANALYSIS_MODEL", "gpt-5.4")
```

- [ ] **Step 2: Add the LLM call wrapper**

Append after the prompt:

```python
import json
from openai import OpenAI

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
```

Move the existing top-of-file `import os` next to the new `import json` block, or leave both — Python is fine with the duplication if `import json` is added at file top. Cleaner: add `import json` at the top alongside `import os`.

- [ ] **Step 3: Add the validator**

Append:

```python
import re

_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PKG_RUN_RE = re.compile(r"^(?:pnpm|npm|yarn|bun)\s+(?:run\s+)?([\w:.-]+)")


def _package_json_scripts(repo_dir: Path, package_path: str) -> set[str]:
    """Return the set of script names defined in package.json under package_path."""
    candidate = repo_dir / package_path / "package.json"
    if not candidate.is_file():
        return set()
    try:
        data = json.loads(candidate.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    scripts = data.get("scripts", {})
    return set(scripts.keys()) if isinstance(scripts, dict) else set()


def validate_plan(plan: dict, repo_dir: str) -> tuple[dict, str, float | None]:
    """Mutate plan in place: downgrade confidence + flag needs_verification on
    steps/env_vars/services that fail sanity checks. Returns (plan, status,
    overall_confidence). status is 'ok' | 'partial' | 'failed'."""
    root = Path(repo_dir)
    packages = plan.get("packages", [])
    if not packages:
        return plan, "partial", None

    surviving_steps = 0
    total_steps = 0
    confidences: list[float] = []

    for pkg in packages:
        pkg_path = pkg.get("path", ".")
        scripts = _package_json_scripts(root, pkg_path)

        for step in pkg.get("steps", []):
            total_steps += 1
            command = step.get("command", "")
            cwd = step.get("cwd", ".")

            cwd_ok = (root / cwd).is_dir() if cwd else True
            cmd_ok = True
            match = _PKG_RUN_RE.match(command.strip())
            if match:
                script_name = match.group(1)
                # `pnpm install` / `npm install` etc. are not "run scripts" — skip.
                if script_name not in {"install", "i", "ci"}:
                    cmd_ok = script_name in scripts

            if not cwd_ok or not cmd_ok:
                step["confidence"] = min(step.get("confidence", 0.5), 0.3)
                step["needs_verification"] = True
            else:
                surviving_steps += 1
            confidences.append(step.get("confidence", 0.5))

        for env in pkg.get("env_vars", []):
            name = env.get("name", "")
            if not _ENV_NAME_RE.match(name):
                env["confidence"] = min(env.get("confidence", 0.5), 0.3)
                env["needs_verification"] = True
            confidences.append(env.get("confidence", 0.5))

        for tool in pkg.get("external_tools", []):
            confidences.append(tool.get("confidence", 0.5))
        for svc in pkg.get("services", []):
            confidences.append(svc.get("confidence", 0.5))
        confidences.append(pkg.get("runtime", {}).get("confidence", 0.5))
        confidences.append(pkg.get("package_manager", {}).get("confidence", 0.5))

    overall = sum(confidences) / len(confidences) if confidences else None
    if total_steps > 0 and surviving_steps == 0:
        return plan, "partial", overall
    return plan, "ok", overall
```

- [ ] **Step 4: Smoke-check the validator with a hand-crafted plan**

```bash
docker compose exec fastapi python3 - <<'PY'
import asyncio
from services.clone_repo import ensure_repo_dir
from services.startup_analysis import validate_plan

async def main():
    repo_dir = await ensure_repo_dir("https://github.com/ThomasBenjaminCook/WattAppWebApp")
    fake_plan = {
        "schema_version": "1.0",
        "summary": "test",
        "is_monorepo": False,
        "packages": [{
            "path": ".", "name": None, "framework": None,
            "runtime": {"language": "node", "version": None, "version_source": None, "confidence": 0.9},
            "package_manager": {"name": "npm", "version": None, "source": None, "confidence": 0.9},
            "external_tools": [], "services": [],
            "env_vars": [
                {"name": "DATABASE_URL", "required": True, "example": None,
                 "sources": [], "confidence": 0.9, "needs_verification": False},
                {"name": "lowercase_var", "required": False, "example": None,
                 "sources": [], "confidence": 0.9, "needs_verification": False},
            ],
            "steps": [
                {"order": 1, "title": "Install", "command": "npm install", "cwd": ".",
                 "explain": "", "confidence": 0.9, "needs_verification": False},
                {"order": 2, "title": "Bogus", "command": "npm run does-not-exist",
                 "cwd": ".", "explain": "", "confidence": 0.9, "needs_verification": False},
            ],
        }],
        "warnings": [],
    }
    plan, status, overall = validate_plan(fake_plan, repo_dir)
    bogus_step = plan["packages"][0]["steps"][1]
    bad_env = plan["packages"][0]["env_vars"][1]
    print(f"status={status} overall={overall:.2f}")
    print(f"bogus_step.confidence={bogus_step['confidence']} needs_verification={bogus_step['needs_verification']}")
    print(f"bad_env.confidence={bad_env['confidence']} needs_verification={bad_env['needs_verification']}")

asyncio.run(main())
PY
```

Expected:
- `status=ok` (the install step still survives).
- `bogus_step.confidence=0.3 needs_verification=True`.
- `bad_env.confidence=0.3 needs_verification=True`.

- [ ] **Step 5: Commit**

```bash
git add services/startup_analysis.py
git commit -m "$(cat <<'EOF'
feat(startup-analysis): add JSON schema, LLM call, validator

Adds the OpenAI response_format JSON schema for the startup plan, a
thin OpenAI client wrapper, and a deterministic validator that
downgrades confidence + flags needs_verification on commands that
don't resolve to real package.json scripts and on env vars whose
names don't match the standard regex.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `analyze_startup_activity`

**Files:**
- Modify: `activities.py` (add `AnalyzeStartupParams` dataclass + `analyze_startup_activity`)

- [ ] **Step 1: Add the dataclass and activity definition**

Add to `activities.py` near the other dataclasses (around line 50):

```python
@dataclass
class AnalyzeStartupParams:
    session_id: str
    repo_url: str
    repo_dir: str
    force: bool = False
```

Add `analyze_startup_activity` after `index_repo_activity` (around line 95). First, extend the imports at the top of `activities.py`:

```python
from services.db import (
    get_pool, get_startup_plan_row, store_chunks, store_dir_summaries,
    upsert_startup_plan,
)
from services.startup_analysis import (
    ANALYSIS_MODEL, build_context, call_llm, validate_plan,
)
```

(Keep `store_chunks`, `store_dir_summaries` already imported.)

Then add the activity:

```python
@activity.defn
async def analyze_startup_activity(params: AnalyzeStartupParams) -> dict:
    """Build a context bundle, call the LLM, validate the plan, persist it,
    and notify the session bus that the plan is updated. Idempotent on
    repo_url unless force=True."""
    if not params.force:
        existing = await get_startup_plan_row(params.repo_url)
        if existing is not None:
            await publish(params.session_id, {
                "type": "data-startup-plan-updated",
                "updatedAt": existing["updated_at"],
            })
            return {"status": existing["analysis_status"], "skipped": True}

    activity.logger.info(
        "Analyzing startup for %s (force=%s)", params.repo_url, params.force,
    )

    bundle = build_context(params.repo_dir)
    activity.logger.info(
        "Context bundle: entries=%d chars=%d truncations=%s",
        len(bundle.entries), bundle.total_chars, bundle.truncations,
    )

    status: str
    overall: float | None
    plan: dict
    error: str | None = None
    try:
        result = call_llm(bundle)
        plan, status, overall = validate_plan(result.plan, params.repo_dir)
        activity.logger.info(
            "LLM ok: prompt_tokens=%d completion_tokens=%d status=%s overall=%s",
            result.prompt_tokens, result.completion_tokens, status, overall,
        )
    except json.JSONDecodeError as exc:
        # One retry: re-call with the same bundle. If it fails again, persist failure.
        activity.logger.warning("LLM JSON parse failed; retrying once: %s", exc)
        try:
            result = call_llm(bundle)
            plan, status, overall = validate_plan(result.plan, params.repo_dir)
        except Exception as exc2:
            activity.logger.error("LLM retry failed: %s", exc2)
            plan, status, overall = {}, "failed", None
            error = f"json_parse: {exc2}"
    except Exception as exc:
        activity.logger.exception("LLM call failed: %s", exc)
        plan, status, overall = {}, "failed", None
        error = str(exc)[:1000]

    await upsert_startup_plan(
        repo_url=params.repo_url,
        plan=plan,
        analysis_status=status,
        overall_confidence=overall,
        model=ANALYSIS_MODEL,
        truncations=bundle.truncations,
        error=error,
    )

    fresh = await get_startup_plan_row(params.repo_url)
    await publish(params.session_id, {
        "type": "data-startup-plan-updated",
        "updatedAt": fresh["updated_at"] if fresh else None,
    })

    return {"status": status, "skipped": False}
```

- [ ] **Step 2: Restart FastAPI to pick up the activity**

```bash
docker compose restart fastapi
```

(Worker restarts in the same lifespan; no separate command.)

- [ ] **Step 3: Smoke-check by invoking the activity body directly (no Temporal)**

The activity body is `async def`; we can call it without the `@activity.defn` runtime if we sidestep `activity.logger`. Easier: call its inner steps individually through the same module:

```bash
docker compose exec fastapi python3 - <<'PY'
import asyncio
from services.clone_repo import ensure_repo_dir
from services.startup_analysis import build_context, call_llm, validate_plan, ANALYSIS_MODEL
from services.db import upsert_startup_plan, get_startup_plan_row

REPO_URL = "https://github.com/ThomasBenjaminCook/WattAppWebApp"

async def main():
    repo_dir = await ensure_repo_dir(REPO_URL)
    bundle = build_context(repo_dir)
    print(f"bundle entries={len(bundle.entries)} chars={bundle.total_chars}")
    result = call_llm(bundle)
    plan, status, overall = validate_plan(result.plan, repo_dir)
    print(f"status={status} overall={overall} packages={len(plan.get('packages', []))}")
    await upsert_startup_plan(REPO_URL, plan, status, overall, ANALYSIS_MODEL,
                               bundle.truncations, None)
    row = await get_startup_plan_row(REPO_URL)
    print(f"persisted: {row['analysis_status']} updated_at={row['updated_at']}")

asyncio.run(main())
PY
```

Expected: `status=ok` (or `partial`), `packages>=1`, persisted row prints with status and timestamp. This call does cost OpenAI tokens (~5–15s). If `status=failed`, fix prompts/schema before moving on.

- [ ] **Step 4: Commit**

```bash
git add activities.py
git commit -m "$(cat <<'EOF'
feat(activities): add analyze_startup_activity

Idempotent activity that builds a context bundle, calls OpenAI with a
JSON-schema constrained response, validates the resulting plan against
the cloned repo, persists it to startup_plans, and publishes a
data-startup-plan-updated event on the session bus. One retry on JSON
parse failure; status='failed' on persistent error rather than crashing
the workflow.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Wire activity into the workflow + worker

**Files:**
- Modify: `workflows.py:6-18` (imports), `workflows.py:22-126` (run + wait loop + new signal)
- Modify: `main.py:14-22` (activity import), `main.py:46-67` (worker `activities=` list)

- [ ] **Step 1: Extend workflow imports and instance state**

In `workflows.py`, expand the `workflow.unsafe.imports_passed_through()` block:

```python
with workflow.unsafe.imports_passed_through():
    from activities import (
        IndexParams,
        ChatParams,
        SessionStatusParams,
        AgentTurnParams,
        AnalyzeStartupParams,
        clone_repo_activity,
        index_repo_activity,
        update_session_status_activity,
        agent_turn_activity,
        analyze_startup_activity,
        cancel_pending_actions_activity,
        resolve_pending_actions_activity,
    )
```

In `CodebaseChatWorkflow.__init__`, add:

```python
self._repo_url: str | None = None
self._session_id: str | None = None
self._recompute_requested = False
self._recompute_reason = ""
```

- [ ] **Step 2: Run the analysis activity sequentially after indexing**

In `CodebaseChatWorkflow.run`, after the `index_repo_activity` block and before flipping `self._status = "ready"`, add:

```python
        await workflow.execute_activity(
            analyze_startup_activity,
            AnalyzeStartupParams(
                session_id=params.session_id,
                repo_url=params.repo_url,
                repo_dir=self._repo_dir,
                force=False,
            ),
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
```

Also stash params into instance state at the very top of `run`, right before the first `update_session_status_activity` call:

```python
        self._repo_url = params.repo_url
        self._session_id = params.session_id
```

- [ ] **Step 3: Add the recompute signal and wait-loop branch**

Add the signal definition alongside the others (after `end_session`):

```python
    @workflow.signal
    def recompute_startup_plan(self, reason: str = "") -> None:
        self._recompute_requested = True
        self._recompute_reason = reason
```

Update the wait condition and the body inside the main loop:

```python
        while not self._ended:
            await workflow.wait_condition(
                lambda: bool(self._user_messages)
                        or bool(self._clarifications)
                        or self._recompute_requested
                        or self._ended
            )
            if self._ended:
                break

            if self._recompute_requested:
                self._recompute_requested = False
                workflow.logger.info(
                    "recompute_startup_plan signal: reason=%r", self._recompute_reason,
                )
                self._recompute_reason = ""
                await workflow.execute_activity(
                    analyze_startup_activity,
                    AnalyzeStartupParams(
                        session_id=params.session_id,
                        repo_url=params.repo_url,
                        repo_dir=self._repo_dir,
                        force=True,
                    ),
                    start_to_close_timeout=timedelta(seconds=120),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
                continue  # re-enter wait_condition

            if self._user_messages:
                # ... (existing body unchanged)
```

Keep the existing `if self._user_messages:` body as it is — only the wait condition and the new branch above it are new.

- [ ] **Step 4: Register the activity in the worker (`main.py`)**

Modify `main.py` imports:

```python
from activities import (
    ChatParams,
    agent_turn_activity,
    analyze_startup_activity,
    cancel_pending_actions_activity,
    clone_repo_activity,
    index_repo_activity,
    resolve_pending_actions_activity,
    update_session_status_activity,
)
```

In the `lifespan` function, append `analyze_startup_activity` to the worker's `activities=` list:

```python
    worker = Worker(
        client,
        task_queue="onboarding-queue",
        workflows=[CodebaseChatWorkflow],
        activities=[
            clone_repo_activity,
            index_repo_activity,
            analyze_startup_activity,
            update_session_status_activity,
            agent_turn_activity,
            cancel_pending_actions_activity,
            resolve_pending_actions_activity,
        ],
    )
```

- [ ] **Step 5: Restart and verify the workflow runs the activity end-to-end**

```bash
docker compose restart fastapi
```

Create a fresh session against a *different* repo (so the existing row from Task 4 step 3 doesn't trigger the idempotency skip):

```bash
SESSION=$(curl -s -X POST http://localhost:8000/sessions \
  -H "Content-Type: application/json" \
  -d '{"repo_url":"https://github.com/jerrym1299/codebase-onboading-agent"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['session_id'])")
echo "session=$SESSION"

# Poll until ready
for i in $(seq 1 60); do
  STATUS=$(curl -s "http://localhost:8000/sessions/$SESSION" | python3 -c "import sys,json;print(json.load(sys.stdin).get('status'))")
  echo "[$i] $STATUS"
  [ "$STATUS" = "ready" ] && break
  sleep 2
done

# Confirm the row exists
docker compose exec postgres psql -U postgres -d codebase_agent -t -c \
  "SELECT analysis_status, overall_confidence FROM startup_plans WHERE repo_url = 'https://github.com/jerrym1299/codebase-onboading-agent';"
```

Expected: status reaches `ready` within ~30 s (clone + index + analyze). The psql query shows one row with `analysis_status` `ok` or `partial`.

- [ ] **Step 6: Commit**

```bash
git add workflows.py main.py
git commit -m "$(cat <<'EOF'
feat(workflow): run analyze_startup_activity on indexing + recompute signal

CodebaseChatWorkflow now runs analyze_startup_activity sequentially
after index_repo_activity, before flipping session.status to 'ready'.
Adds a recompute_startup_plan signal handler that reruns the activity
with force=True from inside the main wait loop. Worker registers the
new activity.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `get_startup_plan` and `recompute_startup_plan` tools

**Files:**
- Modify: `services/tools.py` (append two new tools)

- [ ] **Step 1: Add the read-only `get_startup_plan` tool**

Append to `services/tools.py` (after `ask_user`):

```python
from services.db import get_startup_plan_row


def _format_plan_for_llm(row: dict) -> str:
    """Render a startup_plans row as compact markdown for the LLM."""
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
        lines.append(f"- Runtime: {rt.get('language')} {rt.get('version') or ''} "
                     f"(source: {rt.get('version_source')}, conf {rt.get('confidence')})")
        pm = pkg.get("package_manager", {})
        lines.append(f"- Package manager: {pm.get('name')} {pm.get('version') or ''} "
                     f"(source: {pm.get('source')}, conf {pm.get('confidence')})")
        if pkg.get("external_tools"):
            lines.append("- External tools:")
            for t in pkg["external_tools"]:
                req = "REQUIRED" if t.get("required") else "optional"
                lines.append(f"  - {t.get('name')} ({req}): {t.get('reason')}")
        if pkg.get("services"):
            lines.append("- Services:")
            for s in pkg["services"]:
                lines.append(f"  - {s.get('name')} ({s.get('image') or '?'}, source {s.get('source')})")
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
```

- [ ] **Step 2: Add the `recompute_startup_plan` tool**

Append:

```python
import os
from temporalio.client import Client

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
    """Signal the current session's workflow to recompute the startup plan.
    Returns immediately; the new plan appears within ~10s and triggers a
    data-startup-plan-updated SSE event."""
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
```

- [ ] **Step 3: Verify the tools import and produce text against the existing row**

```bash
docker compose restart fastapi
sleep 3
docker compose exec fastapi python3 - <<'PY'
import asyncio
from services.tools import _format_plan_for_llm
from services.db import get_startup_plan_row

async def main():
    row = await get_startup_plan_row("https://github.com/jerrym1299/codebase-onboading-agent")
    print(_format_plan_for_llm(row)[:600])

asyncio.run(main())
PY
```

Expected: prints a markdown header `# Startup plan (ok|partial, confidence=…)` and at least one `## Package:` section.

- [ ] **Step 4: Commit**

```bash
git add services/tools.py
git commit -m "$(cat <<'EOF'
feat(tools): add get_startup_plan and recompute_startup_plan

get_startup_plan formats the persisted plan as compact markdown for
the LLM, including confidence + needs_verification flags.
recompute_startup_plan signals the current session's workflow to
re-run the analysis activity with force=True.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Bootstrap agent + router/explainer updates

**Files:**
- Modify: `agent_defs.py` (add `bootstrap_agent`, extend `router_agent.handoffs`, extend `explainer_agent.tools`, add sentence to `router_agent.instructions`)

- [ ] **Step 1: Extend imports in `agent_defs.py`**

Replace the existing tools import:

```python
from services.tools import (
    list_files, search_code, read_file,
    find_references, get_dependencies, search_indexed, search_dir_summaries, git_log,
    ask_user,
    get_startup_plan, recompute_startup_plan,
)
```

- [ ] **Step 2: Define `bootstrap_agent`**

Add this above `router_agent` (so it's defined before being referenced in `router_agent.handoffs`):

```python
bootstrap_agent = Agent[Any](
    name="Bootstrap",
    instructions=(
        "You help users get a codebase running locally. You have a precomputed "
        "startup plan for this repo, accessible via `get_startup_plan(repo_url)`. "
        "The plan is the source of truth — start every answer by reading it.\n"
        "\n"
        "Routing rules:\n"
        "1. 'How do I run this' / 'how do I start this' / 'what do I need to install' → "
        "read the plan, summarise: runtime, install command, required services, env vars, "
        "step-by-step. Cite step numbers from the plan.\n"
        "2. 'What env vars do I need' → list `env_vars` from the plan, marking required vs "
        "optional and flagging items where `needs_verification: true` or `example` is null.\n"
        "3. 'Why do I need X' → cite the `sources` array on the relevant plan entry. Use "
        "`read_file` on those sources only if the user asks for more detail.\n"
        "4. 'Re-analyse this repo' / 'I added a new env var, update the plan' → call "
        "`recompute_startup_plan(repo_url, reason)`, tell the user it's running, then re-read "
        "the plan when finished.\n"
        "5. If the plan is missing a value the user is asking about (`needs_verification`, "
        "no example, low confidence), use `ask_user` to clarify — but don't pre-emptively "
        "ask; only when answering depends on it.\n"
        "6. If `analysis_status == 'failed'` (or `get_startup_plan` returns 'no plan "
        "available'), investigate independently. Use `list_files` to find manifests "
        "(package.json, pyproject.toml, go.mod, Cargo.toml, Gemfile, pom.xml, etc.), "
        "`read_file` on them and any `.env.example` / `Dockerfile` / `docker-compose.yml` / "
        "`Makefile` you find, `get_dependencies` for import graphs, and `search_indexed` "
        "for natural-language hints. Synthesise a startup walkthrough from what you find. "
        "Cite `file:line` for every command, env var, and service. Do NOT call "
        "`recompute_startup_plan` automatically — only if the user asks.\n"
        "\n"
        "Always cite step numbers and `file:line` sources. Don't invent commands or env vars. "
        "If the plan doesn't cover something, say so."
    ),
    model="gpt-5.4",
    model_settings=ModelSettings(max_tokens=16384),
    tools=[
        get_startup_plan,
        recompute_startup_plan,
        list_files,
        read_file,
        get_dependencies,
        search_indexed,
        ask_user,
    ],
    handoffs=[explainer_agent, tracer_agent],
    handoff_description=(
        "Hand off to the bootstrap agent for any question about getting the project "
        "running locally: install commands, env vars, required services, dependencies, "
        "Docker setup, dev-server startup, or 'how do I run this'."
    ),
)
```

- [ ] **Step 3: Add `get_startup_plan` to the explainer's tools**

Update the existing `explainer_agent` definition's `tools=` list:

```python
    tools=[search_indexed, search_dir_summaries, list_files, read_file, git_log, ask_user, get_startup_plan],
```

- [ ] **Step 4: Update the router's instructions and handoffs**

Update `router_agent.instructions` to add the bootstrap routing sentence. Replace the existing string with:

```python
    instructions=(
        "You are a router to route the users question to the appropriate agent, you can hand off to the explorer agent to find things in the codebase, the explainer agent to summarise and synthesise information, the tracer agent to trace the execution path of the codebase, or the bootstrap agent for questions about getting the repo running locally. After handing off to one agent you can hand off to another agent. You should ensure you completely and directly answer the users question and pick up all information from the previous agents/related to the question.\n"
        "Any question about getting the repo running locally (install, env vars, services, dev-server, Docker setup, 'how do I run this') goes to the bootstrap agent.\n"
        "Any git-related question (commit history, when/why something changed, recent changes, who/what touched a file, 'what changed recently') goes to the explainer agent — it is the only agent with `git_log`.\n"
        "If you believe the question is ambiguous or unfinished, you can use ask_user to clarify before proceeding. "
        "For example if they say 'trace the flow' without specifying which flow, ask which one. "
        "Or if they ask 'how does the uploading work' and there are multiple upload workflows (e.g. files from computer to server vs server to AWS), ask which upload process they mean."
    ),
```

Update `router_agent.handoffs`:

```python
    handoffs=[explorer_agent, explainer_agent, tracer_agent, bootstrap_agent],
```

- [ ] **Step 5: Restart and verify the agent definitions load**

```bash
docker compose restart fastapi
sleep 3
docker compose exec fastapi python3 -c "from agent_defs import bootstrap_agent, router_agent; print(bootstrap_agent.name, [a.name for a in router_agent.handoffs])"
```

Expected: `Bootstrap ['Explorer', 'Explainer', 'Tracer', 'Bootstrap']`.

- [ ] **Step 6: Commit**

```bash
git add agent_defs.py
git commit -m "$(cat <<'EOF'
feat(agents): add Bootstrap agent + router/explainer wiring

Bootstrap agent is the entry point for 'how do I run this' questions.
Reads the precomputed plan, recomputes via signal when asked, and
falls back to manifest discovery when analysis_status is failed.
Router gains a new handoff and the rule that startup-flavoured
questions route here. Explainer gets read-only get_startup_plan so
it can cite startup info inside broader explanations.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: `GET /sessions/:id/startup-plan`

**Files:**
- Modify: `main.py` (add new endpoint, extend imports)

- [ ] **Step 1: Add the import**

In `main.py`, extend the `services.db` import:

```python
from services.db import (
    CODE_SEARCH_SQL, close_pool, get_pool, get_startup_plan_row, init_schema, store_chunks,
)
```

- [ ] **Step 2: Add the endpoint**

Append to `main.py`:

```python
@app.get("/sessions/{session_id}/startup-plan")
async def get_session_startup_plan_endpoint(session_id: str):
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT repo_url FROM sessions WHERE id = %s", (session_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return {"error": "Session not found."}
    repo_url = row[0]
    plan_row = await get_startup_plan_row(repo_url)
    if plan_row is None:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"status": "pending"})
    return {
        "repo_url": repo_url,
        "plan": plan_row["plan"],
        "analysis_status": plan_row["analysis_status"],
        "overall_confidence": plan_row["overall_confidence"],
        "model": plan_row["model"],
        "truncations": plan_row["truncations"],
        "error": plan_row["error"],
        "updated_at": plan_row["updated_at"],
    }
```

(The local `JSONResponse` import is intentional — keeps the diff small. Move it to the top of the file if you prefer.)

- [ ] **Step 3: Restart and verify happy path**

```bash
docker compose restart fastapi
sleep 4
# Use the session created in Task 5 step 5 (its plan should already exist)
SESSION=$(docker compose exec postgres psql -U postgres -d codebase_agent -t -A -c \
  "SELECT id FROM sessions WHERE repo_url = 'https://github.com/jerrym1299/codebase-onboading-agent' ORDER BY created_at DESC LIMIT 1")
echo "session=$SESSION"
curl -s "http://localhost:8000/sessions/$SESSION/startup-plan" | python3 -m json.tool | head -40
```

Expected: prints `analysis_status: "ok"` (or `"partial"`), a non-null `plan` object, and an `updated_at` timestamp.

- [ ] **Step 4: Verify pending path**

Create a brand new session against a never-analyzed repo, and immediately query the plan endpoint **before** the workflow finishes:

```bash
NEW_SESSION=$(curl -s -X POST http://localhost:8000/sessions \
  -H "Content-Type: application/json" \
  -d '{"repo_url":"https://github.com/sindresorhus/awesome"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['session_id'])")
echo "new=$NEW_SESSION"
# Hit immediately — should be 404
curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:8000/sessions/$NEW_SESSION/startup-plan"
```

Expected: prints `404`. (If indexing is unusually fast you may already see 200 — try a larger repo if so.)

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "$(cat <<'EOF'
feat(api): GET /sessions/:id/startup-plan

Returns the persisted plan for the session's repo. 404 + {status: pending}
when the analysis hasn't been written yet (workflow is still indexing or
analyzing). Decoupled from the chat SSE stream so the frontend can
render the plan in a side panel.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: `POST /sessions/:id/startup-plan/recompute`

**Files:**
- Modify: `main.py` (add endpoint)

- [ ] **Step 1: Add the endpoint**

Append to `main.py`:

```python
@app.post("/sessions/{session_id}/startup-plan/recompute")
async def post_session_startup_recompute_endpoint(session_id: str, payload: dict | None = None):
    reason = ((payload or {}).get("reason") or "").strip()
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT repo_url FROM sessions WHERE id = %s", (session_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return {"error": "Session not found."}
    handle = app.state.temporal_client.get_workflow_handle(f"chat-{session_id}")
    await handle.signal("recompute_startup_plan", reason)
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=202, content={"status": "recomputing", "session_id": session_id})
```

- [ ] **Step 2: Restart and verify the recompute path**

```bash
docker compose restart fastapi
sleep 4
SESSION=$(docker compose exec postgres psql -U postgres -d codebase_agent -t -A -c \
  "SELECT id FROM sessions WHERE repo_url = 'https://github.com/jerrym1299/codebase-onboading-agent' ORDER BY created_at DESC LIMIT 1")

# Capture the current updated_at
BEFORE=$(curl -s "http://localhost:8000/sessions/$SESSION/startup-plan" | python3 -c "import sys,json;print(json.load(sys.stdin)['updated_at'])")
echo "before=$BEFORE"

# Trigger recompute
curl -s -o /dev/null -w "%{http_code}\n" -X POST "http://localhost:8000/sessions/$SESSION/startup-plan/recompute" \
  -H "Content-Type: application/json" -d '{"reason":"manual smoke"}'

# Poll until updated_at advances or 30s elapses
for i in $(seq 1 30); do
  AFTER=$(curl -s "http://localhost:8000/sessions/$SESSION/startup-plan" | python3 -c "import sys,json;print(json.load(sys.stdin)['updated_at'])")
  if [ "$AFTER" != "$BEFORE" ]; then
    echo "after=$AFTER (advanced after ${i}s)"
    break
  fi
  sleep 1
done
```

Expected: HTTP 202, then `updated_at` advances within ~15 s.

- [ ] **Step 3: Verify coalesce-on-concurrent**

```bash
# Fire two recomputes in parallel — both should accept (the workflow
# coalesces internally; there's no per-request rejection).
(curl -s -o /dev/null -w "1:%{http_code}\n" -X POST "http://localhost:8000/sessions/$SESSION/startup-plan/recompute" -H "Content-Type: application/json" -d '{}' &
 curl -s -o /dev/null -w "2:%{http_code}\n" -X POST "http://localhost:8000/sessions/$SESSION/startup-plan/recompute" -H "Content-Type: application/json" -d '{}' &
 wait)
```

Expected: both lines print `202`. The workflow's `_recompute_requested` flag means the second signal is harmless if the first run hasn't finished.

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "$(cat <<'EOF'
feat(api): POST /sessions/:id/startup-plan/recompute

Signals the session's workflow to re-run analyze_startup_activity with
force=True. Returns 202; new plan available via GET /startup-plan in a
few seconds. Concurrent calls are coalesced by the workflow.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: SSE event for plan updates is consumable from `POST /messages`

**Files:** None (verification-only).

The activity already publishes `data-startup-plan-updated` to the per-session bus (Task 4 step 1). Existing SSE subscribers in `POST /sessions/:id/messages` forward whatever lands in the queue. This task just verifies that path.

- [ ] **Step 1: Trigger a recompute while a chat SSE stream is open and confirm the event is forwarded**

In one terminal, open a chat stream:

```bash
SESSION=$(docker compose exec postgres psql -U postgres -d codebase_agent -t -A -c \
  "SELECT id FROM sessions WHERE repo_url = 'https://github.com/jerrym1299/codebase-onboading-agent' ORDER BY created_at DESC LIMIT 1")

curl -N -X POST "http://localhost:8000/sessions/$SESSION/messages" \
  -H "Content-Type: application/json" \
  -d '{"content":"hi"}' | tee /tmp/sse.log &
SSE_PID=$!
sleep 2
```

In a second shell (still in the repo dir):

```bash
SESSION=$(docker compose exec postgres psql -U postgres -d codebase_agent -t -A -c \
  "SELECT id FROM sessions WHERE repo_url = 'https://github.com/jerrym1299/codebase-onboading-agent' ORDER BY created_at DESC LIMIT 1")
curl -s -X POST "http://localhost:8000/sessions/$SESSION/startup-plan/recompute" -H "Content-Type: application/json" -d '{}'
```

Wait until the chat stream has emitted its `finish` (or kill the sse pid after ~30s):

```bash
sleep 30
kill $SSE_PID 2>/dev/null
grep -c '"type": "data-startup-plan-updated"' /tmp/sse.log
```

Expected: at least `1`. (If the chat completed before the recompute event fired, re-run with a longer-running message like `"explain how the codebase is organised in detail"`.)

- [ ] **Step 2: No commit (verification only)**

---

## Task 11: End-to-end smoke test script

**Files:**
- Create: `scripts/test_startup_plan.py`

This is the "integration test" equivalent in this codebase's convention — a runnable Python script using `urllib`, modelled after `scripts/test_ask_question.py`.

- [ ] **Step 1: Write the script**

```python
"""
test_startup_plan.py — smoke test for the startup-analysis feature.

Creates a session, polls until status == 'ready', fetches the startup
plan, asserts the structured shape, then exercises:
  - GET /sessions/:id/startup-plan (happy path)
  - POST /sessions/:id/startup-plan/recompute (advances updated_at)
  - POST /sessions/:id/messages "how do I run this?" (Bootstrap agent
    should be in the SSE handoff stream and answer with steps)

Usage:
    python3 scripts/test_startup_plan.py
    python3 scripts/test_startup_plan.py --repo https://github.com/foo/bar
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_REPO = "https://github.com/ThomasBenjaminCook/WattAppWebApp"


def _post(url: str, body: dict, timeout: int = 60) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


def _get(url: str, timeout: int = 60) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


def _stream_messages(base: str, session_id: str, content: str,
                     stop_after_seconds: int = 60) -> list[dict]:
    """POST a message and read the SSE response into a list of parsed events."""
    req = urllib.request.Request(
        f"{base}/sessions/{session_id}/messages",
        data=json.dumps({"content": content}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    events: list[dict] = []
    deadline = time.time() + stop_after_seconds
    with urllib.request.urlopen(req, timeout=stop_after_seconds + 5) as resp:
        for raw in resp:
            if time.time() > deadline:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[len("data:"):].strip())
            except json.JSONDecodeError:
                continue
            events.append(event)
            if event.get("type") == "finish":
                break
    return events


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    args = ap.parse_args()

    print(f"Creating session for {args.repo} ...")
    code, body = _post(f"{args.base}/sessions", {"repo_url": args.repo})
    assert code == 200, f"create session failed: {code} {body}"
    session_id = body["session_id"]
    print(f"  session_id={session_id}")

    # Poll until ready (or fail).
    print("Polling status ...")
    deadline = time.time() + 180
    while time.time() < deadline:
        code, body = _get(f"{args.base}/sessions/{session_id}")
        status = body.get("status")
        print(f"  status={status}")
        if status == "ready":
            break
        if status == "ended":
            sys.exit("Session ended before ready.")
        time.sleep(2)
    else:
        sys.exit("Timeout waiting for ready.")

    # Plan present?
    code, plan_body = _get(f"{args.base}/sessions/{session_id}/startup-plan")
    assert code == 200, f"GET startup-plan failed: {code} {plan_body}"
    plan = plan_body["plan"]
    assert plan_body["analysis_status"] in ("ok", "partial"), \
        f"unexpected analysis_status: {plan_body['analysis_status']}"
    assert plan.get("packages"), "plan.packages is empty"
    pkg = plan["packages"][0]
    assert pkg.get("steps"), f"package has no steps: {pkg}"
    print(f"  plan: status={plan_body['analysis_status']} "
          f"packages={len(plan['packages'])} steps={len(pkg['steps'])}")

    # Recompute and check updated_at advances.
    before = plan_body["updated_at"]
    code, _ = _post(f"{args.base}/sessions/{session_id}/startup-plan/recompute", {})
    assert code == 202, f"recompute returned {code}"
    print("  recompute requested; polling updated_at ...")
    deadline = time.time() + 60
    while time.time() < deadline:
        code, plan_body = _get(f"{args.base}/sessions/{session_id}/startup-plan")
        if plan_body.get("updated_at") != before:
            print(f"  updated_at advanced: {plan_body['updated_at']}")
            break
        time.sleep(2)
    else:
        sys.exit("updated_at never advanced after recompute.")

    # Ask the bootstrap agent how to run the repo.
    print("Streaming chat: 'how do I run this repo locally?' ...")
    events = _stream_messages(args.base, session_id, "how do I run this repo locally?",
                              stop_after_seconds=120)
    handoffs = [e for e in events if e.get("type") == "data-handoff"]
    text_parts = [e for e in events if e.get("type") in ("text", "text-delta")]
    print(f"  events={len(events)} handoffs={len(handoffs)} text_chunks={len(text_parts)}")
    bootstrap_seen = any(h.get("agent") == "Bootstrap" for h in handoffs)
    assert bootstrap_seen, f"Bootstrap agent never engaged. handoffs={handoffs}"

    print("\nALL CHECKS PASSED.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the script end-to-end**

```bash
docker compose exec fastapi python3 scripts/test_startup_plan.py \
  --repo https://github.com/ThomasBenjaminCook/WattAppWebApp
```

Expected final line: `ALL CHECKS PASSED.` Total runtime ~60–120 s.

If the assertion `Bootstrap agent never engaged` fires, inspect the handoffs printed before the failure. The router's instructions may need a tweak — but lock that fix into Task 7 not here.

- [ ] **Step 3: Commit**

```bash
git add scripts/test_startup_plan.py
git commit -m "$(cat <<'EOF'
test: add startup-plan end-to-end smoke script

Mirrors scripts/test_ask_question.py — creates a session, polls
status, asserts the plan shape, exercises recompute, and confirms
the Bootstrap agent engages on a 'how do I run this' message.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: Final happy-path + failure-path verification

**Files:** None (verification + final commit if anything was tweaked).

- [ ] **Step 1: Run the smoke test against the canonical test repo**

```bash
docker compose exec fastapi python3 scripts/test_startup_plan.py
```

Expected: `ALL CHECKS PASSED.`

- [ ] **Step 2: Failure-path check — bootstrap agent fallback on a synthetic failure**

Inject `analysis_status='failed'` for a fresh repo to exercise rule 6 of the bootstrap prompt (independent investigation).

```bash
# 1. Create a session and wait for ready.
SESSION=$(curl -s -X POST http://localhost:8000/sessions \
  -H "Content-Type: application/json" \
  -d '{"repo_url":"https://github.com/sindresorhus/awesome"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['session_id'])")
for i in $(seq 1 90); do
  S=$(curl -s "http://localhost:8000/sessions/$SESSION" | python3 -c "import sys,json;print(json.load(sys.stdin).get('status'))")
  [ "$S" = "ready" ] && break
  sleep 2
done

# 2. Forcibly mark the plan as failed.
docker compose exec postgres psql -U postgres -d codebase_agent -c \
  "UPDATE startup_plans SET analysis_status='failed', error='synthetic failure', plan='{}'::jsonb WHERE repo_url='https://github.com/sindresorhus/awesome';"

# 3. Ask the bootstrap agent and confirm it falls back to manifest discovery.
curl -N -X POST "http://localhost:8000/sessions/$SESSION/messages" \
  -H "Content-Type: application/json" \
  -d '{"content":"how do I run this repo locally?"}' | head -c 4000
```

Expected: the SSE stream shows a `data-handoff` to `Bootstrap`, plus tool calls for `list_files` / `read_file` (rule 6 fallback). The final answer should mention concrete file names like `readme.md`, NOT a hallucinated command. (For this particular repo there is no runnable code; the agent should say so.)

- [ ] **Step 3: Inspect the activity logs for budget/truncation behavior**

```bash
docker compose logs fastapi --tail 200 | grep -E "(Analyzing startup|Context bundle|LLM ok|LLM JSON parse|LLM call failed)"
```

Expected: at least one "Analyzing startup" + "Context bundle" + "LLM ok" line per fresh repo. No unhandled exceptions.

- [ ] **Step 4: If anything was tweaked, commit; otherwise no-op**

```bash
git status
```

Expected: clean working tree (all earlier tasks committed).

---

## Self-review notes

After writing this plan, I checked:

1. **Spec coverage** — every section of the spec is implemented:
   - Architecture diagram → Tasks 4–9.
   - `startup_plans` schema → Task 1.
   - `plan` JSONB schema → Task 3 step 1.
   - Context bundle priorities → Task 2.
   - LLM call (raw OpenAI client + response_format) → Task 3 step 2.
   - Validation policy (downgrade + flag, keep step) → Task 3 step 3.
   - `analyze_startup_activity` semantics + idempotency → Task 4.
   - Workflow integration (sequential + recompute signal + wait branch) → Task 5.
   - REST surface (GET 404 pending, POST 202) → Tasks 8–9.
   - Coalesce concurrent recomputes → Task 9 step 3.
   - SSE `data-startup-plan-updated` → Tasks 4 + 10.
   - Bootstrap agent + explainer read access + router routing → Task 7.
   - Failure-mode rule 6 (independent investigation) → Task 7 step 2 + Task 12 step 2.
   - End-to-end test → Task 11.

2. **Type/name consistency** — checked `AnalyzeStartupParams(session_id, repo_url, repo_dir, force)` matches between activities.py, workflows.py, and the worker registration. Helper names `get_startup_plan_row` / `upsert_startup_plan` consistent across db.py, tools.py, and activities.py. Signal name `recompute_startup_plan` consistent across workflow definition, agent tool, and HTTP endpoint.

3. **No placeholders** — every step contains the actual code or commands, no "TBD", no "implement similar to above". The system prompt for the LLM in Task 3 is a concrete string.

4. **Open spec questions resolved** — the spec flagged three open implementation questions:
   - *Where the SSE publish lives*: resolved as "inside `analyze_startup_activity`" (Task 4); no separate `publish_event_activity` introduced.
   - *Prompt wording*: locked to a concrete `SYSTEM_PROMPT` constant in Task 3 step 1.
   - *Truncations exposed to the agent*: yes, surfaced in `_format_plan_for_llm` (Task 6 step 1) as a `_Note: dropped from context: ..._` line.
