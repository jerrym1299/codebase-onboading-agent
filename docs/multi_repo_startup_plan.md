# Multi-Repo Startup Plan — Implementation Plan

## Goal

Extend the existing single-repo bootstrap pipeline to ingest N GitHub repos in one session, infer how they wire together, and emit one consolidated, ordered startup plan covering the entire application.

The chat experience also generalises so the user can ask questions about any repo in the session without scoping ceremony.

## Pipeline (target shape)

```
HTTP                   Temporal workflow                    Per-repo (parallel)              Cross-repo (sequential)         Consolidator
────                   ─────────────────                    ────────────────                 ──────────────────              ────────────
POST /sessions ─────▶  CodebaseChatWorkflow                 clone_repo_activity              build_graph_activity           consolidator_agent
  { repo_urls:[...] }    indexing                           index_repo_activity              (orchestration parse +          (Runner.run_streamed,
                         │                                  analyze_startup_activity ─────┐  exposed↔consumed match +        text-delta SSE)
                         ├─ asyncio.gather per repo ───▶    extract_boundaries_activity ──┤  edge classification +           uses lookup tools +
                         │                                                                │  topo sort)                       file tools to verify
                         │                                                                │                                  produces final markdown
                         ▼                                                                ▼                                  + persists app_startup_plans
                         build_graph_activity ◀── inputs: [BoundaryReport, StartupPlan, repo_dir, repo_url] per repo
                         │
                         ▼
                         consolidate_plan_activity (streamed)
                         │
                         ▼
                         ready ─────▶ chat loop (multi-repo aware)
```

## Decisions log (Q1–Q15)


| #   | Decision                    | Pick                                                                                                                                                                                                                                                |
| --- | --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Q1  | API surface                 | Single endpoint, accepts `repo_urls: [...]`. Existing single-repo `{ repo_url }` payload still accepted as one-element shorthand.                                                                                                                   |
| Q2  | Session ↔ repos             | Join table `session_repos(session_id, repo_url)`. Drop `sessions.repo_url`. No `position` column.                                                                                                                                                   |
| Q3  | Workflow shape              | Single `CodebaseChatWorkflow` with parallel activities via `asyncio.gather`. Abort-all on any per-repo failure with a useful error.                                                                                                                 |
| Q4  | Extractor JSON shape        | `Agent(output_type=BoundaryReport)` (Pydantic) — SDK-native structured output.                                                                                                                                                                      |
| Q5  | Extractor tools             | No `ask_user`. Tools = `list_files`, `read_file`, `get_dependencies`, `search_code`, `search_indexed`. Ambiguities go in JSON.                                                                                                                      |
| Q6  | Per-repo plan vs boundaries | Keep both. `analyze_startup_activity` (existing, non-agentic) → `startup_plans`. New `extract_boundaries_activity` (agentic) → `repo_boundaries`. Both feed the consolidator.                                                                       |
| Q7  | Per-repo phase ordering     | Sequential within each repo: `clone → index → analyze_startup → extract_boundaries`. Extractor receives the startup plan in its developer prompt as a prior. Drop `runtime`/`startup` from `BoundaryReport` (covered by plan).                      |
| Q8  | Boundary kinds              | Trim to: `http` (exposed/consumed), `db` (consumed), `dev_proxy` (top-level), `required_services` (top-level), `ambiguities` (top-level). Drop `graphql`, `websocket`, `shared_types`, and all queue kinds for v1.                                  |
| Q9  | App plan cache key          | `app_startup_plans` PK = `repo_set_hash` = `sha256(\n.join(sorted(rstrip-slashed repo_urls)))`. `sessions.app_plan_hash` foreign key. No content-hashing of inputs (don't worry about repo updates / staleness for now).                            |
| Q10 | Graph shape                 | Bipartite: `RepoNode` + `InfraNode`. Dedupe infra by `(kind, target_env)`.                                                                                                                                                                          |
| Q11 | Consolidator tools          | `get_repo_boundaries`, `get_repo_startup_plan`, `list_files`, `read_file`, `search_indexed`. **No** `ask_user` — unresolved ambiguities surface in the markdown's Caveats section.                                                                  |
| Q12 | Endpoints                   | C — repurpose `/sessions/:id/startup-plan`* for app-level + add per-repo diagnostic endpoints `GET /sessions/:id/repos/:repo_url/{startup-plan,boundaries}`. App-level recompute reruns the whole pipeline.                                         |
| Q13 | SSE events                  | A + D. Per-stage, per-repo events emitted by each per-repo activity. Consolidator streamed via `Runner.run_streamed` with text-delta + tool events (same shape as chat turns).                                                                      |
| Q14 | Orchestration detection     | Inside the matcher activity. Pure-code, deterministic, scoped per-repo (no parent-dir-spanning support in v1).                                                                                                                                      |
| Q15 | Multi-repo chat             | A. Inject all `(repo_url, repo_dir)` pairs into the developer prompt; tool surface unchanged. Add `get_app_startup_plan(session_id)` to `bootstrap_agent`.                                                                                          |


## Data model changes

### Drop

- `sessions.repo_url` (replaced by `session_repos` join).
- `sessions_repo_idx` index (no longer needed).

### New tables / columns

```sql
-- session ↔ repo association
CREATE TABLE IF NOT EXISTS session_repos (
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    repo_url   TEXT NOT NULL,
    PRIMARY KEY (session_id, repo_url)
);

CREATE INDEX IF NOT EXISTS session_repos_repo_idx ON session_repos (repo_url);

-- per-repo wire boundaries (new agentic extractor output)
CREATE TABLE IF NOT EXISTS repo_boundaries (
    repo_url           TEXT PRIMARY KEY,
    report             JSONB NOT NULL,        -- BoundaryReport.model_dump()
    analysis_status    TEXT NOT NULL CHECK (analysis_status IN ('ok', 'partial', 'failed')),
    model              TEXT NOT NULL,
    error              TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- consolidated, app-level plan keyed on repo set
CREATE TABLE IF NOT EXISTS app_startup_plans (
    repo_set_hash          TEXT PRIMARY KEY,
    repo_urls              TEXT[] NOT NULL,                -- denormalised; the set this hash covers
    plan_markdown          TEXT NOT NULL,                  -- consolidator's final document
    graph                  JSONB NOT NULL,                 -- typed dependency graph
    ambiguities            JSONB NOT NULL DEFAULT '[]',    -- unresolved + matcher-flagged
    orchestration_findings JSONB NOT NULL DEFAULT '[]',    -- per-repo orchestration parse results
    analysis_status        TEXT NOT NULL CHECK (analysis_status IN ('ok', 'partial', 'failed')),
    model                  TEXT NOT NULL,
    error                  TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- session → app plan pointer
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS app_plan_hash TEXT;
CREATE INDEX IF NOT EXISTS sessions_app_plan_hash_idx ON sessions (app_plan_hash);
```

### Keep (unchanged)

- `code_chunks`, `dir_summaries`, `messages`, `pending_actions`.
- `startup_plans` — still produced per repo by `analyze_startup_activity`. Schema unchanged.

## `BoundaryReport` schema (Pydantic)

```python
# services/boundary_extractor.py
from typing import Literal
from pydantic import BaseModel, Field

# Exposed edges
class ExposedHttp(BaseModel):
    kind: Literal["http"] = "http"
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "ANY"]
    path: str
    handler: str | None = None      # file:line or file path

# Consumed edges
class ConsumedHttp(BaseModel):
    kind: Literal["http"] = "http"
    target_env: str | None = None    # "ATHENS_API_URL"
    resolved: str | None = None      # "http://localhost:4001"
    resolved_from: str | None = None # ".env.example", "code default", ...
    path: str | None = None

class ConsumedDb(BaseModel):
    kind: Literal["db"] = "db"
    engine: Literal["postgres", "mysql", "sqlite", "mongodb", "redis", "other"]
    target_env: str | None = None
    resolved: str | None = None
    resolved_from: str | None = None

# Top-level fields
class DevProxy(BaseModel):
    from_path: str
    to_env: str | None = None
    to_resolved: str | None = None
    config_file: str

class RequiredService(BaseModel):
    kind: Literal["postgres", "mysql", "redis", "elasticsearch", "other"]
    via: str | None = None           # env var name

class Ambiguity(BaseModel):
    field: str                       # JSON path, e.g. "consumed[0].resolved"
    reason: str

class BoundaryReport(BaseModel):
    repo_url: str
    exposed: list[ExposedHttp] = Field(default_factory=list)
    consumed: list[ConsumedHttp | ConsumedDb] = Field(default_factory=list)
    dev_proxy: list[DevProxy] = Field(default_factory=list)
    required_services: list[RequiredService] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
```

Note: `runtime` / `startup` intentionally absent — covered by `startup_plans` row, fed to the extractor as prior context (Q7).

## Graph data model (matcher output)

```python
# services/dependency_graph.py
from typing import Literal
from pydantic import BaseModel

class RepoNode(BaseModel):
    kind: Literal["repo"] = "repo"
    id: str                           # repo_url
    name: str

class InfraNode(BaseModel):
    kind: Literal["infra"] = "infra"
    id: str                           # f"{infra_kind}:{target_env}"
    infra_kind: Literal["postgres", "mysql", "redis", "elasticsearch", "other"]
    target_env: str | None

class Edge(BaseModel):
    source: str                       # node id
    target: str                       # node id
    edge_type: Literal["hard_runtime", "soft_runtime", "shared_infra"]
    confidence: float                 # 0..1
    evidence: list[str]               # e.g. ["repo-A.consumed[0]", "repo-B.exposed[2]"]
    match_reason: str                 # "url+port", "env_name_heuristic", "db_engine"

class CycleBreak(BaseModel):
    cycle: list[str]                  # node ids in order
    broken_edge: tuple[str, str]      # (source, target)
    reason: str

class DependencyGraph(BaseModel):
    nodes: list[RepoNode | InfraNode]
    edges: list[Edge]
    topo_order: list[list[str]]       # list of parallel groups
    cycle_breaks: list[CycleBreak]
```

## Workflow shape

```python
# workflows.py — CodebaseChatWorkflow.run (sketch)
self._repo_urls = params.repo_urls    # ChatParams now carries list
self._status = "indexing"
await update_session_status("indexing")

async def per_repo(repo_url: str):
    repo_dir = await execute_activity(clone_repo_activity, CloneParams(repo_url, session_id), ...)
    await execute_activity(index_repo_activity, IndexParams(repo_url, repo_dir, session_id), ...)
    await execute_activity(analyze_startup_activity, AnalyzeStartupParams(session_id, repo_url, repo_dir, force=False), ...)
    await execute_activity(extract_boundaries_activity, ExtractBoundariesParams(session_id, repo_url, repo_dir), ...)
    return repo_dir

repo_dirs = await asyncio.gather(*[per_repo(u) for u in self._repo_urls])
self._repo_dirs = dict(zip(self._repo_urls, repo_dirs))

# Cross-repo, sequential
graph = await execute_activity(build_graph_activity, BuildGraphParams(session_id, self._repo_urls, self._repo_dirs), ...)
await execute_activity(consolidate_plan_activity, ConsolidateParams(session_id, repo_set_hash, self._repo_urls, self._repo_dirs), ...)

self._status = "ready"
await update_session_status("ready")

# Chat loop unchanged in shape; agent_turn_activity now reads session_repos and injects all
```

### New / changed activities


| Activity                           | Params                                                               | Timeout | Retries | Purpose                                                                                                                                                                                                                     |
| ---------------------------------- | -------------------------------------------------------------------- | ------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `clone_repo_activity`              | `CloneParams(repo_url, session_id)`                                  | 120s    | 3       | (changed) takes `session_id`, publishes `data-repo-progress` events.                                                                                                                                                        |
| `index_repo_activity`              | `IndexParams(repo_url, repo_dir, session_id)`                        | 600s    | 2       | (changed) `session_id` for progress events.                                                                                                                                                                                 |
| `analyze_startup_activity`         | unchanged                                                            | 120s    | 2       | unchanged                                                                                                                                                                                                                   |
| `extract_boundaries_activity`      | `ExtractBoundariesParams(session_id, repo_url, repo_dir)`            | 240s    | 2       | (new) agentic extractor → `repo_boundaries`. Reads `startup_plans` row as prior.                                                                                                                                            |
| `build_graph_activity`             | `BuildGraphParams(session_id, repo_urls, repo_dirs)`                 | 60s     | 2       | (new) deterministic; orchestration parse + matcher + topo sort. Output persisted into `app_startup_plans.graph` + `.orchestration_findings` + `.ambiguities` (with placeholder `plan_markdown=""` until consolidator runs). |
| `consolidate_plan_activity`        | `ConsolidateParams(session_id, repo_set_hash, repo_urls, repo_dirs)` | 600s    | 1       | (new) streamed; runs `consolidator_agent`, persists final `plan_markdown`.                                                                                                                                                  |
| `update_session_status_activity`   | unchanged                                                            | 30s     | 3       | unchanged                                                                                                                                                                                                                   |
| `agent_turn_activity`              | unchanged params                                                     | 300s    | 1       | (changed internally) reads `session_repos`, injects all repos into developer prompt.                                                                                                                                        |
| `cancel_pending_actions_activity`  | unchanged                                                            | 15s     | 3       | unchanged                                                                                                                                                                                                                   |
| `resolve_pending_actions_activity` | unchanged                                                            | 15s     | 3       | unchanged                                                                                                                                                                                                                   |


### Workflow signals (kept + new)


| Signal                                      | Effect                                                                       |
| ------------------------------------------- | ---------------------------------------------------------------------------- |
| `user_message(content)`                     | unchanged                                                                    |
| `clarification_response(pending_id, value)` | unchanged                                                                    |
| `end_session()`                             | unchanged                                                                    |
| `recompute_startup_plan(reason)`            | (changed) reruns the **whole** pipeline (per-repo + matcher + consolidator). |


## New module: `services/dependency_graph.py` (matcher)

Pure code, no LLM. Single entrypoint called from `build_graph_activity`:

```python
def build_graph(
    repos: list[tuple[str, str, BoundaryReport, dict]]   # (url, repo_dir, boundaries, startup_plan)
) -> tuple[DependencyGraph, list[OrchestrationFinding]]:
    ...
```

Internal stages, in order:

1. **Detect orchestration** — for each `repo_dir`, deterministically parse `docker-compose*.{yml,yaml}`, `Procfile`, `Tiltfile`, `skaffold.yaml`, `devcontainer.json`, root `Makefile` `up`/`dev`/`start` targets. Emit `OrchestrationFinding(repo_url, file, parsed_services, parsed_dependencies)`.
2. **Build infra nodes** — union of `required_services` across all repos, plus services from each repo's compose. Dedupe by `(kind, target_env)`. (`postgres` via `DATABASE_URL` and `postgres` via `ANALYTICS_DB_URL` → two distinct nodes.)
3. **Build repo nodes** — one per `repo_url`.
4. **Add infra edges** — for each repo's `consumed.kind=db` and `required_services`, add a `shared_infra` edge `repo → infra_node`. Edge type = `shared_infra` (treated as hard for ordering).
5. **Match `consumed.http` ↔ `exposed.http`** across repos:
  - Normalise both sides (lowercase host, strip trailing slash, resolve localhost variants).
  - First pass: match on resolved value (port + path prefix).
  - Fallback: env-name heuristic (`ATHENS_API_URL` → repo whose `name` contains `athens`).
  - Use `dev_proxy` entries as ground truth: a frontend's `dev_proxy[].to_env` resolves to a backend repo if that repo's `startup_plan.steps[].command` exposes a matching port.
  - Each match → `Edge(soft_runtime, confidence)` with `evidence` listing the source `consumed[]` and target `exposed[]` indices.
  - Unmatched `consumed[].kind=http` with only a `target_env` (no `resolved`) → push to `ambiguities` for the consolidator.
6. **Classify hard vs soft** — heuristics:
  - All `shared_infra` edges → effectively hard for ordering (infra must be up first).
  - HTTP edges where the source is a frontend (detected via presence of `dev_proxy` entries pointing at the target) → `soft_runtime`.
  - HTTP edges where the source's `startup_plan` has a healthcheck/migration/connection step that hits the target → `hard_runtime`.
  - Default for HTTP: `soft_runtime`.
7. **Topological sort** — Kahn's algorithm over hard edges (`hard_runtime` + `shared_infra`). Soft edges become preferences within the order, not constraints. Output `topo_order` as a list of parallel groups (each group = nodes that can start simultaneously).
8. **Cycle detection + breaking** — if a cycle exists in hard edges, find soft edges within the SCC and demote one to soft, recording a `CycleBreak`. If no soft edge in the SCC, mark the cycle as unresolvable in `ambiguities`.

All flagged ambiguities + low-confidence edges flow into `app_startup_plans.ambiguities` and are also written into the consolidator's developer prompt.

## New agent: `consolidator_agent`

```python
# agent_defs.py (new)
consolidator_agent = Agent[Any](
    name="Consolidator",
    instructions=(
        "You produce a final, ordered startup plan for an application that spans multiple repos. "
        "You receive (in the developer prompt): the dependency graph (typed nodes + edges + topological order + cycle-break records), "
        "matcher-flagged ambiguities, orchestration findings, and the list of repos with their local paths and indexed urls. "
        "Use the lookup tools (`get_repo_boundaries`, `get_repo_startup_plan`) to read each repo's data. "
        "Use `list_files`, `read_file`, `search_indexed` to verify cross-repo claims when the matcher's confidence is low. "
        "DO NOT silently override the matcher's topo order — if you change it, explain why in Caveats. "
        "Output a single markdown document with these sections in order:\n"
        "  # Startup plan: <app name or repo set summary>\n"
        "  ## Prerequisites — required infra (postgres, redis, etc.) and version requirements\n"
        "  ## Env vars — grouped by repo, marked required/optional\n"
        "  ## Steps — one numbered step per ordered group from the topo sort, parallel commands grouped\n"
        "  ## Dependency graph — Mermaid diagram of nodes and typed edges\n"
        "  ## Caveats — ambiguous edges, cycle-breaking decisions, low-confidence matches, anything to verify\n"
    ),
    model="gpt-5.4",
    model_settings=ModelSettings(max_tokens=16384),
    tools=[
        get_repo_boundaries,        # new tool
        get_repo_startup_plan,      # new tool
        list_files,
        read_file,
        search_indexed,
    ],
)
```

## New tools (`services/tools.py`)

```python
@function_tool
async def get_repo_boundaries(repo_url: str) -> str:
    """Return the BoundaryReport JSON for a repo, or 'no boundaries available'."""

@function_tool
async def get_repo_startup_plan(repo_url: str) -> str:
    """Return the per-repo startup plan JSON for a repo, or 'no plan available'."""
    # already exists as get_startup_plan; rename or alias

@function_tool
async def get_app_startup_plan(session_id: str) -> str:
    """Return the consolidated app-level startup plan markdown for this session, or
    'no app plan available'. Used by bootstrap_agent to answer 'how do I run the
    whole stack' for multi-repo sessions."""
```

## New agent: `boundary_extractor_agent`

```python
# agent_defs.py (new)
boundary_extractor_agent = Agent[BoundaryReport](
    name="BoundaryExtractor",
    instructions=(
        "You produce a strict BoundaryReport for one repository. The repo's local path and indexed repo_url "
        "are in the developer prompt, alongside the per-repo startup plan that has already been generated. "
        "Use the startup plan as context — runtime, package manager, env vars are already known there. "
        "Your job: surface WIRE BOUNDARIES — what HTTP routes the repo exposes, what HTTP/DB endpoints it consumes, "
        "what dev-server proxies are configured, what infra services are required. "
        "For each consumed entry, surface BOTH the symbolic env var name AND any resolved value you can find "
        "(.env, .env.example, code defaults, docker-compose, deployment configs). "
        "Symbolic-only targets go into `ambiguities`. "
        "DO NOT invent paths or routes — only what you can ground in real files. "
        "Use `list_files`, `read_file`, `get_dependencies`, `search_code`, `search_indexed` to investigate. "
        "Once you have a complete report, return it."
    ),
    output_type=BoundaryReport,
    model="gpt-5.4",
    model_settings=ModelSettings(max_tokens=16384),
    tools=[list_files, read_file, get_dependencies, search_code, search_indexed],
)
```

## HTTP endpoints

### Modified

- `POST /sessions { repo_urls: [...] }` — also accepts legacy `{ repo_url }` (treated as `[repo_url]`). Inserts session row, inserts N rows in `session_repos`. Computes `repo_set_hash`, sets `sessions.app_plan_hash`. Starts workflow with `ChatParams(session_id, repo_urls)`.
- `GET /sessions/:id/startup-plan` — now returns the **app-level** plan from `app_startup_plans` (joined via `sessions.app_plan_hash`). Shape: `{ repo_urls, plan_markdown, graph, ambiguities, orchestration_findings, analysis_status, model, error, updated_at }`.
- `POST /sessions/:id/startup-plan/recompute` — signals workflow `recompute_startup_plan`; reruns the whole pipeline.
- `POST /sessions/:id/startup-plan/export` — thin wrapper now: reads `app_startup_plans.plan_markdown`, runs `write_markdown_pdf`. **No** verification agent (the consolidator already verified). Returns `{ session_id, repo_urls, markdown, pdf_base64 }`.

### New (per-repo diagnostics)

- `GET /sessions/:id/repos/:repo_url/startup-plan` — returns the per-repo `startup_plans` row.
- `GET /sessions/:id/repos/:repo_url/boundaries` — returns the per-repo `repo_boundaries` row.

`:repo_url` in path is URL-encoded.

### Existing chat endpoints — unchanged signatures

- `POST /sessions/:id/messages { content }` — unchanged. Internally `agent_turn_activity` now reads `session_repos` and injects all repos.
- `GET /sessions/:id/messages` — unchanged.
- `GET /sessions/:id` — unchanged.

## SSE event additions

Existing events unchanged: `text-delta`, `text`, `tool-input-available`, `tool-output-available`, `data-handoff`, `data-needs-input`, `finish`.

New during the bootstrap phase:


| Event                   | Emitted by                           | Payload                                       |
| ----------------------- | ------------------------------------ | --------------------------------------------- |
| `data-repo-progress`    | per-repo activity                    | `{ repo_url, stage: "cloning"                 |
| `data-graph-built`      | `build_graph_activity`               | `{ node_count, edge_count, ambiguity_count }` |
| `data-app-plan-updated` | `consolidate_plan_activity` (at end) | `{ updatedAt, repo_set_hash }`                |


The consolidator's run streams `text-delta` / `tool-input-available` / `tool-output-available` events identical to a chat turn (separate stream so the FE can render "the plan is being written" UI distinctly from a chat response — distinguish via a `data-consolidator-started` event before streaming begins).

## Multi-repo chat changes

`agent_turn_activity` ([activities.py:215](activities.py#L215)) loop:

1. Replace `SELECT repo_url FROM sessions WHERE id = %s` with `SELECT repo_url FROM session_repos WHERE session_id = %s`.
2. Resolve each repo's `repo_dir` via `ensure_repo_dir`.
3. `prepend_repo_context` becomes:
  ```
   "Repos in this session:\n"
   "- {name}: local={repo_dir}, indexed_url={repo_url}\n"
   "  ..."
   "When you call search_indexed/search_dir_summaries, pass the repo_url that matches the question.\n"
   "When you call read_file/list_files, use the local path of the relevant repo.\n"
   "If the question is ambiguous about which repo, use ask_user to clarify."
  ```
4. `current_session_id` ContextVar still set (unchanged).

Agent instruction tweaks:

- `router_agent`: add a sentence about disambiguating which repo via `ask_user` when unclear.
- `bootstrap_agent`: add `get_app_startup_plan` to its tools; instruction note: "for cross-stack 'how do I run everything' questions, use `get_app_startup_plan(session_id)`. For single-repo questions, use `get_startup_plan(repo_url)`."
- `explorer_agent`, `explainer_agent`, `tracer_agent`: instructions get one-line note about picking the right `repo_url` when calling per-repo tools.

## Implementation order (sequenced steps)

Each step is independently testable. Keep PRs/commits to one step each.

### Step 1 — Schema migration

- Edit [services/db.py](services/db.py): add `session_repos`, `repo_boundaries`, `app_startup_plans` to `SCHEMA_SQL`. Add `ALTER TABLE sessions ADD COLUMN app_plan_hash`. Drop `sessions.repo_url` (user said wipe-the-db is fine, so just remove the column from `SCHEMA_SQL`).
- Update `init_schema()` if needed. Wipe local DB and re-run.

### Step 2 — Session creation accepts array

- Edit [main.py:198](main.py#L198) `POST /sessions`: accept `repo_urls`/`repo_url`. Insert session row, insert `session_repos` rows, compute `repo_set_hash`, set `sessions.app_plan_hash`.
- Update `ChatParams` dataclass in [activities.py](activities.py): `repo_url: str` → `repo_urls: list[str]`.
- Workflow signature change: `CodebaseChatWorkflow.run(params: ChatParams)` reads `params.repo_urls`.
- Test: existing single-repo `curl` flow still works (one-element array internally).

### Step 3 — Workflow parallelises per-repo phase (no extractor yet)

- Edit [workflows.py](workflows.py): wrap `clone → index → analyze_startup` per repo, run with `asyncio.gather`. `abort-all` semantics handled by `gather` re-raising.
- Add `session_id` to `clone_repo_activity` and `index_repo_activity` params; emit `data-repo-progress` events from inside.
- Test: create a session with 2 repos via curl, watch SSE on the messages stream — see two clone/index/analyze cycles, both repos end up indexed and have `startup_plans` rows.

### Step 4 — `repo_boundaries` table + new agentic extractor

- Add `services/boundary_extractor.py`:
  - `BoundaryReport` Pydantic model and its sub-types (per Q8 trimmed taxonomy).
  - `boundary_extractor_agent` definition (in `agent_defs.py`).
  - `run_boundary_extractor(repo_url, repo_dir, startup_plan_json) -> BoundaryReport` — wraps `Runner.run` with a developer prompt that includes the startup plan as prior.
- Add `services/db.py` functions: `get_repo_boundaries_row`, `upsert_repo_boundaries`.
- Add `extract_boundaries_activity` to [activities.py](activities.py); register in worker activities list in [main.py](main.py) lifespan.
- Wire `extract_boundaries_activity` into the workflow's per-repo branch (after `analyze_startup`, sequential).
- Test: per-repo `repo_boundaries` row populated for the test repo. Inspect via `psql` or add `GET /sessions/:id/repos/:repo_url/boundaries` here (early).

### Step 5 — Matcher (deterministic graph)

- Add `services/dependency_graph.py`:
  - Pydantic types (`RepoNode`, `InfraNode`, `Edge`, `CycleBreak`, `DependencyGraph`).
  - `parse_orchestration(repo_dir) -> list[OrchestrationFinding]` — YAML/Procfile parsing.
  - `build_graph(repos) -> (DependencyGraph, list[OrchestrationFinding], list[Ambiguity])`.
- Add `build_graph_activity` to [activities.py](activities.py); register.
- Persist into `app_startup_plans` with `plan_markdown=""` placeholder, `analysis_status='partial'`.
- Test unit: `pytest` over `services/dependency_graph.py` with hand-built `BoundaryReport` fixtures.
- Test e2e: 2-repo session, watch DB for `app_startup_plans` row with graph + ambiguities populated.

### Step 6 — Consolidator agent (streamed)

- Add `consolidator_agent` to [agent_defs.py](agent_defs.py).
- Add `get_repo_boundaries`, `get_repo_startup_plan`, `get_app_startup_plan` tools to [services/tools.py](services/tools.py).
- Add `consolidate_plan_activity` to [activities.py](activities.py): build developer prompt with graph+ambiguities+orchestration+repo list, run `Runner.run_streamed`, stream events to bus, persist final markdown to `app_startup_plans.plan_markdown`, set `analysis_status='ok'`, publish `data-app-plan-updated`.
- Wire into workflow after `build_graph_activity`.
- Test: 2-repo session, observe text-delta events on the messages SSE during consolidation, final `plan_markdown` populated.

### Step 7 — App-level endpoints + per-repo diagnostics

- Edit [main.py](main.py):
  - `GET /sessions/:id/startup-plan` → reads `app_startup_plans` via `sessions.app_plan_hash`.
  - `POST /sessions/:id/startup-plan/recompute` — already exists; signal handler in workflow now reruns the whole pipeline.
  - `POST /sessions/:id/startup-plan/export` — thin wrapper around `write_markdown_pdf` over stored markdown.
  - New: `GET /sessions/:id/repos/:repo_url/startup-plan`, `GET /sessions/:id/repos/:repo_url/boundaries`.
- Test: full curl flow, all four endpoint shapes return correct data.

### Step 8 — Multi-repo chat

- Edit `agent_turn_activity` in [activities.py:215](activities.py#L215): read `session_repos`, build multi-repo developer prompt.
- Update agent instructions in [agent_defs.py](agent_defs.py) (router, bootstrap, explorer, explainer, tracer) per the multi-repo notes above.
- Add `get_app_startup_plan` to `bootstrap_agent`'s tool list.
- Test: 2-repo session, ask "where is the entry point?" — agent should clarify which repo or pick correctly. Ask "how do I run the whole stack?" — bootstrap agent calls `get_app_startup_plan`.

### Step 9 — Recompute signal widens scope

- Edit [workflows.py](workflows.py): `recompute_startup_plan` signal handler now reruns clone/index/analyze/extract for each repo, then matcher, then consolidator (force=True for analyze/extract).
- Test: `POST /sessions/:id/startup-plan/recompute`, observe progress events for all repos.

### Step 10 — Polish

- Update [API.md](API.md) to document new endpoints + payloads + SSE events.
- Update [CLAUDE.md](CLAUDE.md) with the new pipeline diagram, new tables, new activities, new agents.
- Smoke test the full flow against multiple real repos (the `WattAppWebApp` test repo + at least one other).

## Open items / explicitly deferred

- **Parent-dir-spanning orchestration** (one umbrella `docker-compose.yml` referencing multiple repos as subdirs). Punted — current scope is independent repos.
- **Repo update / staleness** of `app_startup_plans` when an upstream `repo_boundaries` changes. Punted — manual recompute only.
- **Monorepo workspace expansion** (`pnpm-workspace.yaml`, `turbo.json` workspace package detection as separate logical units). Punted to a later iteration.
- `**shared_types`, `graphql`, `websocket`** boundary kinds. Trimmed from v1; can be added without breaking existing data by appending to the `exposed` / `consumed` discriminated unions.
- `**ask_user` on consolidator.** Excluded from v1; cross-repo ambiguities surface in the markdown's Caveats section. Revisit if users frequently end up running the plan and hitting unanswered questions.
- **Per-repo `recompute` endpoint** (`POST /sessions/:id/repos/:repo_url/recompute`). Not part of v1; only app-level recompute exists.
- **Cache invalidation on schema bumps** (when `BoundaryReport` shape evolves). Add a `boundaries_version` constant later and include in the hash if needed.

