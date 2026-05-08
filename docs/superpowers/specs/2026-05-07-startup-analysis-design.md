# Startup analysis — design

**Status:** Approved for implementation planning
**Date:** 2026-05-07
**Author:** Jerry Mahajan (with Claude)

## Goal

Given a GitHub repo, produce a structured "how to run this locally" plan covering runtime, package manager, install/build/dev commands, required services, external tools, environment variables, and migrations. Expose the plan to the frontend (for a checklist UI) and to a new `bootstrap_agent` (for chat answers).

The plan is computed once during indexing and is re-runnable on demand.

## Non-goals (v1)

- Sandbox execution / `npm install --dry-run` verification (planned for a later phase).
- Plan diffing or version history — recomputes overwrite in place.
- Partial recompute (e.g. "just env vars"); always whole-plan.
- Frontend-driven inline plan edits.
- Streaming the analysis output (single-shot LLM call).
- A hand-curated framework registry (LLM handles dispatch).
- Automatic re-analysis when the clone updates; only explicit recompute.

## Architecture

```
HTTP                       Workflow                       Activities
─────                      ─────────────────────          ──────────────────────────────
POST /sessions     ──▶     indexing                       clone_repo_activity
                            ├─ index_repo_activity ──▶    (existing)
                            └─ analyze_startup_activity   (NEW)
                           ready                          update_session_status_activity
                           user_message ──▶               agent_turn_activity (router → bootstrap_agent)
POST .../recompute ─signal▶ recompute_startup_plan ──▶    analyze_startup_activity (force=True)
GET  .../startup-plan       (reads startup_plans row)
```

Workflow runs `analyze_startup_activity` sequentially after `index_repo_activity`, before flipping the session to `ready`. The LLM call adds ~5–15 s to first-time indexing; subsequent sessions for the same `repo_url` short-circuit because the row already exists.

### New code surface

| Path | Role |
|---|---|
| `services/startup_analysis.py` | Context-bundle builder, structured-output LLM call, schema validator. |
| `activities.py::analyze_startup_activity` | Temporal activity wrapping the above. Idempotent on `repo_url` unless `force=True`. |
| `services/db.py::SCHEMA_SQL` | New `startup_plans` table + helpers (`get_startup_plan_row`, `upsert_startup_plan`). |
| `agent_defs.py::bootstrap_agent` | Peer to explorer/explainer/tracer. |
| `services/tools.py` | `get_startup_plan(repo_url)` (read-only, given to bootstrap **and** explainer); `recompute_startup_plan(repo_url, reason)` (bootstrap-only, signals workflow). |
| `workflows.py` | New signal `recompute_startup_plan`; wait-loop branch handles it. |
| `main.py` | `GET /sessions/:id/startup-plan`, `POST /sessions/:id/startup-plan/recompute`. |

## Data model

```sql
CREATE TABLE IF NOT EXISTS startup_plans (
    repo_url           TEXT PRIMARY KEY,
    plan               JSONB NOT NULL,
    analysis_status    TEXT NOT NULL,            -- 'ok' | 'partial' | 'failed'
    overall_confidence REAL,                      -- 0..1, null if failed
    model              TEXT NOT NULL,             -- 'gpt-5.4'
    truncations        TEXT[] DEFAULT '{}',       -- buckets dropped from context
    error              TEXT,                      -- non-null only when status='failed'
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

One row per `repo_url` (same scoping as `code_chunks` and `dir_summaries`). Recomputes upsert.

### `plan` JSONB schema (enforced via OpenAI `response_format` JSON schema)

```jsonc
{
  "schema_version": "1.0",
  "summary": "Next.js + Postgres app. Single package. Requires Docker for local DB.",
  "is_monorepo": false,
  "packages": [
    {
      "path": ".",
      "name": "wattapp",
      "framework": "nextjs",
      "runtime": {
        "language": "node",          // node|python|go|rust|ruby|java|other
        "version": "20",
        "version_source": ".nvmrc:1",
        "confidence": 0.95
      },
      "package_manager": {
        "name": "pnpm",              // npm|pnpm|yarn|bun|pip|poetry|uv|cargo|go|...
        "version": "9",
        "source": "pnpm-lock.yaml",
        "confidence": 0.98
      },
      "external_tools": [
        { "name": "docker", "required": true, "reason": "docker-compose.yml present", "confidence": 0.95 }
      ],
      "services": [
        { "name": "postgres", "image": "postgres:16", "source": "docker-compose.yml:5", "confidence": 0.95 }
      ],
      "env_vars": [
        {
          "name": "DATABASE_URL",
          "required": true,
          "example": "postgres://postgres:postgres@localhost:5432/app",
          "sources": [".env.example:3", "prisma/schema.prisma:6"],
          "confidence": 0.9,
          "needs_verification": false
        }
      ],
      "steps": [
        {
          "order": 1,
          "title": "Install dependencies",
          "command": "pnpm install",
          "cwd": ".",
          "explain": "Lockfile pnpm-lock.yaml indicates pnpm.",
          "confidence": 0.98,
          "needs_verification": false
        }
      ]
    }
  ],
  "warnings": ["No .env.example found — env vars inferred from `process.env.X` references"]
}
```

Notes:

- `packages` is always an array (path `.` for single-package repos) so monorepo and single-package consumers share one code path.
- `confidence` (0..1) appears on every fact that can be uncertain.
- `needs_verification: true` is the "agent should mention this" flag — set when validation downgrades a step or when an env var has no example.
- `sources` carry `file:line` (or just `file` for whole-file evidence) and back the bootstrap agent's "why do I need this?" answers.

## Analysis pipeline (`services/startup_analysis.py`)

```
1. Build context bundle (deterministic, ≤ ~32k chars).
2. Call OpenAI client with response_format JSON schema.
3. Validate against schema + sanity rules; downgrade confidence on failures.
4. Persist to startup_plans (upsert on repo_url).
```

### Context bundle

Reads files only — no embeddings, no chunking. Inclusion rules:

| Bucket | Files | Cap |
|---|---|---|
| Always | `README*`, `package.json`, `pnpm-workspace.yaml`, `turbo.json`, `nx.json`, `lerna.json`, `pyproject.toml`, `requirements*.txt`, `Pipfile`, `poetry.lock`, `uv.lock`, `setup.py`, `setup.cfg`, `go.mod`, `Cargo.toml`, `Gemfile`, `composer.json`, `pom.xml`, `build.gradle*` | full |
| Env hints | `.env.example`, `.env.sample`, `.env.template`, `.env.dist` | full |
| Infra | `Dockerfile*`, `docker-compose*.yml`, `compose*.yml`, `Procfile`, `Makefile`, `justfile`, `.tool-versions`, `.nvmrc`, `.python-version`, `.ruby-version` | full |
| CI signal | `.github/workflows/*.yml` (`run:` lines), `.gitlab-ci.yml`, `vercel.json`, `vercel.ts`, `netlify.toml`, `railway.toml`, `fly.toml`, `wrangler.toml` | head 200 lines each |
| Migrations | `prisma/schema.prisma`, `alembic.ini`, `knexfile*`, top-level `migrations/` listing | full / listing only |
| Repo skeleton | Top-2-level directory tree (file names only) | listing |

If the bundle exceeds budget, drop in reverse priority order and record dropped buckets in `truncations[]`.

### LLM call

- Client: raw `openai.OpenAI` (not Agents SDK) — this is one-shot synthesis, not tool-using.
- Model: `gpt-5.4`.
- Temperature: 0.1.
- `response_format`: JSON schema matching the structure above.
- System prompt: "You are a senior developer specialized in onboarding. Produce a structured startup plan from the provided repository context."
- Single round-trip; no streaming.

### Validation pass (deterministic, in Python)

For every `step`, `env_var`, and `service`:

- Commands of form `<pnpm|npm|yarn|bun> run <script>` must reference a script that exists in the corresponding `package.json`. Otherwise: `confidence: low`, `needs_verification: true`. Step is **kept**, not dropped.
- `cwd` must exist in the cloned repo. Otherwise: downgrade as above.
- Env var `name` must match `^[A-Z][A-Z0-9_]*$`. Mismatches downgraded.
- If zero steps survive validation: `analysis_status='partial'`. If JSON parsing fails after one retry: `analysis_status='failed'`, `error=<msg>`, `plan={}`.

## API surface

### `GET /sessions/:id/startup-plan`

```
200 → { plan, analysis_status, overall_confidence, model, updated_at, truncations }
404 → { status: 'pending' }   // session is still indexing or analysis hasn't run yet
```

Decoupled from chat; FE renders the plan in a side panel without touching SSE.

### `POST /sessions/:id/startup-plan/recompute`

```
Request:  { reason?: string }
Response: 202 { status: 'recomputing' }
```

Signals the workflow with `recompute_startup_plan(reason)`. Idempotent: a second request while a recompute is in flight returns 202 immediately and is coalesced.

## Workflow changes (`workflows.py`)

```python
@workflow.signal
def recompute_startup_plan(self, reason: str = "") -> None:
    self._recompute_requested = True
    self._recompute_reason = reason
```

Wait loop adds a branch:

```python
await workflow.wait_condition(
    lambda: bool(self._user_messages)
            or bool(self._clarifications)
            or self._recompute_requested
            or self._ended
)

if self._recompute_requested:
    self._recompute_requested = False
    await workflow.execute_activity(
        analyze_startup_activity,
        AnalyzeStartupParams(repo_url=self._repo_url, repo_dir=self._repo_dir, force=True),
        start_to_close_timeout=timedelta(seconds=120),
        retry_policy=RetryPolicy(maximum_attempts=2),
    )
    await workflow.execute_activity(
        publish_event_activity,
        PublishEventParams(self._session_id, {"type": "data-startup-plan-updated"}),
        start_to_close_timeout=timedelta(seconds=15),
    )
```

Plan-update broadcasts go through the per-session event bus so any open chat SSE stream sees `data-startup-plan-updated` and can refetch `GET /startup-plan`. (If `publish_event_activity` does not yet exist, this can be folded into `analyze_startup_activity` — implementation plan to decide.)

## Activity timeouts and retries

| Activity | start_to_close | retry max_attempts |
|---|---|---|
| `analyze_startup_activity` | 120 s | 2 |

Aligns with existing patterns in `activities.py` (`clone` 120/3, `index` 600/2, `agent_turn` 300/1).

## Bootstrap agent (`agent_defs.py`)

```python
bootstrap_agent = Agent[Any](
    name="Bootstrap",
    instructions=(
        "You help users get a codebase running locally. You have a precomputed startup plan "
        "for this repo, accessible via `get_startup_plan(repo_url)`. The plan is the source "
        "of truth — start every answer by reading it.\n"
        "\n"
        "Routing rules:\n"
        "1. 'How do I run this' / 'how do I start this' / 'what do I need to install' → "
        "   read the plan, summarise: runtime, install command, required services, env vars, "
        "   step-by-step. Cite step numbers from the plan.\n"
        "2. 'What env vars do I need' → list `env_vars` from the plan, marking required vs "
        "   optional and flagging items where `needs_verification: true` or `example` is null.\n"
        "3. 'Why do I need X' → cite the `sources` array on the relevant plan entry. Use "
        "   `read_file` on those sources only if the user asks for more detail.\n"
        "4. 'Re-analyse this repo' / 'I added a new env var, update the plan' → call "
        "   `recompute_startup_plan(repo_url, reason)`, tell the user it's running, then "
        "   re-read the plan when finished.\n"
        "5. If the plan is missing a value the user is asking about (`needs_verification`, "
        "   no example, low confidence), use `ask_user` to clarify — but don't pre-emptively "
        "   ask; only when answering depends on it.\n"
        "6. If `analysis_status == 'failed'` (or `get_startup_plan` returns no row), "
        "   investigate independently. Use `list_files` to find manifests (package.json, "
        "   pyproject.toml, go.mod, Cargo.toml, Gemfile, pom.xml, etc.), `read_file` on "
        "   them and any `.env.example` / `Dockerfile` / `docker-compose.yml` / `Makefile` "
        "   you find, `get_dependencies` for import graphs, and `search_indexed` for "
        "   natural-language hints. Synthesise a startup walkthrough from what you find. "
        "   Cite `file:line` for every command, env var, and service. Do NOT call "
        "   `recompute_startup_plan` automatically — only if the user asks.\n"
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
    handoff_description=(
        "Hand off to the bootstrap agent for any question about getting the project "
        "running locally: install commands, env vars, required services, dependencies, "
        "Docker setup, dev-server startup, or 'how do I run this'."
    ),
)
```

### Updates to existing agents

- `explainer_agent.tools` gains `get_startup_plan` (read-only). No prompt change required.
- `router_agent.handoffs` gains `bootstrap_agent`.
- `router_agent` instructions add: *"Any question about getting the repo running locally (install, env vars, services, dev-server, Docker setup) goes to the bootstrap agent."*
- `bootstrap_agent.handoffs = [explainer_agent, tracer_agent]` (e.g., user wants deeper architecture explanation or to trace a specific symbol's usage).

### Tools (`services/tools.py`)

```python
@function_tool
async def get_startup_plan(repo_url: str) -> str:
    """Return a markdown-formatted summary of the persisted startup plan for the
    LLM. Returns "no plan available" if the row is missing or status='failed'."""

@function_tool
async def recompute_startup_plan(repo_url: str, reason: str = "") -> str:
    """Signal the current session's workflow to recompute the plan. Returns
    immediately; new plan appears in a few seconds and triggers a
    data-startup-plan-updated event on the SSE stream."""
```

`recompute_startup_plan` reads `current_session_id` from the existing `ContextVar` to find the workflow handle (`chat-<session_id>`) — same pattern as `ask_user`.

## SSE event mapping

`agent_turn_activity` already emits `data-handoff` for any agent transition, which covers handoff to the bootstrap agent without changes.

The new event published by the workflow on plan completion or recompute:

```jsonc
{ "type": "data-startup-plan-updated", "updatedAt": "2026-05-07T14:20:31Z" }
```

Published to the per-session event bus (not appended to a `messages` row). FE refetches `GET /startup-plan` on receipt.

## Failure modes

| Failure | Outcome |
|---|---|
| LLM returns invalid JSON | One retry. If still bad: `analysis_status='failed'`, `error=<msg>`, `plan={}`. Workflow continues to `ready`. |
| LLM returns schema-valid but nonsense | Per-step validation downgrades to `confidence: low` + `needs_verification: true`. If 0 steps survive: `analysis_status='partial'`. |
| Context bundle exceeds budget | Drop in reverse priority order; record in `truncations[]`. Plan still written. |
| No detectable manifests | `analysis_status='partial'`, `warnings` populated. Bootstrap agent's failure-mode rule kicks in (rule 6). |
| OpenAI 429 / 5xx | Activity-level Temporal retries (max 2). Then `analysis_status='failed'`. Workflow still flips to `ready`. |
| Recompute called before first run finishes | Coalesced via `_recompute_requested` flag. |
| Activity timeout (>120 s) | Persist `failed`. User can retry via recompute. |

In every case the workflow reaches `ready` — chat is never blocked by analysis problems.

## Observability

- Standard activity logging: input params (`repo_url`, `force`), elapsed time, status, model, prompt tokens, completion tokens.
- Truncations and per-step validation downgrades logged at `WARN`.
- Failed analysis logs the raw LLM output (truncated to 2000 chars) at `ERROR`.
- No new metrics dashboard for v1; Temporal UI activity history + Postgres row inspection are sufficient.

## Testing

Test repo per CLAUDE.md: `https://github.com/ThomasBenjaminCook/WattAppWebApp`.

| Case | Steps |
|---|---|
| Happy path | `POST /sessions` → poll until `ready` → `GET /startup-plan` → assert non-null `plan` with `packages[].steps[]` populated. |
| Recompute | `POST /startup-plan/recompute` → poll `GET` → assert `updated_at` advanced. |
| Failure path | Use a known-bad repo URL or empty repo → assert `analysis_status='failed'` and bootstrap agent answers using fallback rule 6. |
| Agent path | `POST /sessions/:id/messages` "how do I run this?" → consume SSE → assert `data-handoff` to Bootstrap and an answer citing the plan. |
| Pre-ready GET | Hit `GET /startup-plan` while `status='indexing'` → assert 404 with `{ status: 'pending' }`. |
| Concurrent recompute | Fire two recompute requests in <100 ms → assert second returns 202 immediately and only one analysis runs. |

## Out of scope (reiterated)

Sandbox execution; plan diffing/history; partial recompute; FE inline edits; analysis streaming; framework registry; auto-recompute on repo update.

## Open implementation questions (defer to plan stage)

- Whether `publish_event_activity` is added as a separate activity or the SSE event is published from inside `analyze_startup_activity` directly (workflow determinism rules out publishing from the workflow itself).
- Exact prompt wording for the analyzer LLM call — to be iterated against the test repo.
- Whether the `truncations` list is exposed to the agent or only logged.
