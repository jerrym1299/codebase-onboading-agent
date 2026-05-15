# codebase-onboarding-agent

Backend API for long-lived AI chat sessions over **one or more** GitHub repositories. Point the agent at a list of repo URLs: it clones, AST-chunks, embeds, and summarises each codebase in parallel, extracts per-repo wire boundaries, runs a deterministic cross-repo matcher to build a typed dependency graph, and finally streams a consolidated app-level startup plan as markdown. Then it answers questions through a router-of-agents loop that streams `file:line` citations to the frontend over SSE. Single-repo sessions are just the N=1 case. This repo is the backend only.

## Core flow at a glance

```
HTTP (FastAPI)            Temporal workflow                  Activities                       Agents (OpenAI Agents SDK)
─────────────             ────────────────────               ──────────────────               ─────────────────────────
main.py                   CodebaseChatWorkflow               PER REPO (asyncio.gather)        router_agent
  POST /sessions ──▶        indexing                           clone_repo_activity              ├── explorer_agent
  POST /…/messages ─sig▶     ├─ _run_pipeline ─┐                index_repo_activity              ├── explainer_agent
  GET  /…/messages           │   per repo:     │                analyze_startup_activity         ├── tracer_agent
  GET  /…/startup-plan        │     clone       │                extract_boundaries_activity      ├── bootstrap_agent
  POST /…/recompute           │     index      │                                                  ├── boundary_extractor_agent
  POST /…/export              │     analyze   ─┘ ──▶          CROSS-REPO (sequential)           └── consolidator_agent
  GET  /…/repos/:url/         │     extract                     build_graph_activity
       startup-plan           │                                  consolidate_plan_activity
  GET  /…/repos/:url/         ├─ ready                                                          Runner.run_streamed
       boundaries             ├─ chat loop ──▶                  agent_turn_activity ──────▶     + SQLiteSession
  SSE stream ◀── event_bus    └─ recompute_startup_plan         cancel/resolve_pending_actions
                                  signal → _run_pipeline(force=True)
```

One **Temporal workflow per session**, one **chat thread per workflow**, **N repos per session**. The workflow owns the session lifecycle (`indexing` → `ready` → `ended`), runs the full per-repo + cross-repo pipeline once on start (and again on `recompute_startup_plan` signal), and routes every user message to a single `agent_turn_activity` that streams events to a per-session pubsub which the FastAPI SSE endpoint forwards to the client.

## Repo layout

| Path | Role |
|---|---|
| `main.py` | FastAPI app. Owns lifespan (init Postgres schema, start Temporal worker on task queue `onboarding-queue`), HTTP endpoints, SSE streaming. |
| `workflows.py` | `CodebaseChatWorkflow` — durable per-session orchestrator. Owns `_run_pipeline(force)` which the initial run + the `recompute_startup_plan` signal both call. Signals: `user_message`, `clarification_response`, `end_session`, `recompute_startup_plan`. Queries: `get_status`, `get_pending`. |
| `activities.py` | Nine Temporal activities (`clone_repo_activity`, `index_repo_activity`, `analyze_startup_activity`, `extract_boundaries_activity`, `build_graph_activity`, `consolidate_plan_activity`, `update_session_status_activity`, `agent_turn_activity`, `cancel_pending_actions_activity`, `resolve_pending_actions_activity`) + dataclass params (`CloneParams`, `IndexParams`, `ChatParams`, `AgentTurnParams`, `SessionStatusParams`, `AnalyzeStartupParams`, `ExtractBoundariesParams`, `BuildGraphParams`, `ConsolidateParams`). Streaming agent turn + streamed consolidator both live here. |
| `agent_defs.py` | The seven agents (`router`, `explorer`, `explainer`, `tracer`, `bootstrap`, `boundary_extractor`, `consolidator`) with their tool sets, prompts, handoffs. All on `gpt-5.4` with `max_tokens=16384`. |
| `services/clone_repo.py` | `git clone` with optional `GITHUB_TOKEN` injection; idempotent `ensure_repo_dir`. |
| `services/walk_repo.py` | Filtered file iteration (skips `node_modules`, `.git`, build dirs; keeps source/markup/docs extensions). |
| `services/chunk_and_embed.py` | Tree-sitter AST chunking for Python/JS/TS/TSX, markdown heading split, whole-file fallback for config/HTML/CSS/shell. Token-budget splitter + OpenAI batch embedding. |
| `services/dir_summaries.py` | LLM-generated (`gpt-5.4`) per-directory natural-language summaries, then embedded for high-level semantic browsing. |
| `services/db.py` | Postgres + pgvector pool, schema bootstrap, upserts and getters for repo connections, index jobs, content-addressed manifests, exact-search lines, sessions, startup plans, repo boundaries, and app startup plans. |
| `services/exact_search.py` | Builds line-level exact-search inventories from the content-addressed manifest. |
| `services/embedding_cache.py` | Hydrates and stores cached embeddings keyed by repo + embedding hash. |
| `services/repo_manifest.py` | Builds stable file and chunk manifests for incremental indexing. |
| `services/indexing.py` | Shared manifest/cache-aware indexing entrypoint used by Temporal activities and workers. |
| `services/startup_analysis.py` | Builds a curated context bundle (`README`, manifests, env templates, infra/CI files, top-level skeleton; budget-capped at ~32k chars), calls OpenAI (`STARTUP_ANALYSIS_MODEL`, default `gpt-5.4`) with a JSON-schema-constrained response → produces the `startup_plans.plan` JSON. |
| `services/boundary_extractor.py` | Pydantic `BoundaryReport` schema (exposed/consumed HTTP, consumed DB, dev_proxy, required_services, ambiguities) + the developer-prompt builder used by `extract_boundaries_activity`. |
| `services/dependency_graph.py` | Deterministic cross-repo matcher (no LLM). Parses orchestration files, dedupes infra nodes by `(kind, target_env)`, matches HTTP edges by port+path then env-name fallback, classifies hard vs soft, runs `graphlib.TopologicalSorter` over hard edges, and breaks cycles by demoting hard edges. Returns a typed `DependencyGraph`. |
| `services/pdf_output.py` | `write_markdown_pdf` — converts the consolidated app-level markdown to PDF (used by `POST /sessions/:id/startup-plan/export`). |
| `services/tools.py` | Function tools exposed to the agents (file/code search, exact search, semantic search, references, deps, git log, `ask_user`, `get_startup_plan`, `recompute_startup_plan`, `get_repo_startup_plan`, `get_repo_boundaries`, `get_app_startup_plan`). Owns the `current_session_id` ContextVar. |
| `services/event_bus.py` | In-process per-session asyncio `Queue` fan-out used to bridge the activity → SSE response. |
| `scripts/` | Manual debugging helpers (`show_chunks.py`, `show_ast.py`, `test_ask_question.py`, `test_startup_plan.py`, `test_dependency_graph.py`). |

## Vector indexing pipeline

All embeddings — for chunks, directory summaries, and live queries — use OpenAI `text-embedding-3-large` (3072-dim, stored as pgvector `halfvec(3072)`). Tokenisation budget is enforced with `tiktoken.encoding_for_model("text-embedding-3-large")`; ordinary chunks target a smaller window and hard-fail before provider calls if anything still exceeds the model limit.

Indexing runs through `index_repo_activity`. Each run builds a content-addressed file/chunk manifest, reuses cached embeddings keyed by `embedding_sha256`, embeds only misses, and skips directory-summary regeneration when the manifest is unchanged.

1. **Clone** (`services/clone_repo.py::ensure_repo_dir`) — clones into `/repos/<repo_name>` if not already present; reuses the directory across sessions of the same repo. Tokens are stripped from any error logs.
2. **Walk** (`services/walk_repo.py::collect_file_paths`) — depth-first walk skipping vendored/build dirs (`node_modules`, `.next`, `dist`, …) and keeping source + markup + docs extensions only.
3. **AST chunk** (`services/chunk_and_embed.py::chunk_file_list` → `_dispatch_extract`) — extension-dispatched semantic chunking:
   - **Python** (`extract_chunks_from_file`): module docstring, grouped imports block, top-level functions (incl. decorated), classes. Classes <60 lines stay whole; larger classes split into a header chunk + one chunk per method (with `parent_class` set).
   - **JS/JSX/TS/TSX** (`extract_js_chunks`): top-level functions, classes, interfaces, type aliases, enums, lexical/variable declarations. `export` wrappers are unwrapped to inspect the inner declaration. Imports are grouped into one chunk. Files with no extractable top-level decls (mostly JSX) fall back to a single whole-file chunk.
   - **Markdown** (`extract_markdown_chunks`): split on `#`/`##`/`###` headings into named sections.
   - **JSON / TOML / YAML / INI / env / CSS / HTML / shell** (`extract_whole_file_chunk`): treated as one chunk per file with a typed label (`config`, `stylesheet`, `markup`, `shell`).
4. **Token-budget split** (`split_oversized`) — chunks are recut at line or token boundaries with overlap, preserving `chunk_type`, `name`, `parent_class` and recording `part N` in the name.
5. **Manifest + exact lines + cache** (`services/repo_manifest.py`, `services/exact_search.py`, `services/embedding_cache.py`) — compute relative-path file hashes, stable chunk hashes, embedding hashes, and a non-empty line inventory. Cached embeddings are hydrated before new embedding calls.
6. **Prefix + embed** — each chunk's `embedding_text` is `"# File: <relative path>\n# Class: <parent>\n# <chunk_type>: <name>\n<content>"`. Cache misses are batched into `client.embeddings.create(model="text-embedding-3-large")` → 3072-dim vectors.
7. **Upsert** (`services/db.py::store_chunks`, `store_repo_text_lines`) — chunks upsert by `(repo_url, chunk_sha256)`, stale chunks are pruned on replacement indexes, and exact-search lines are rewritten only for files whose content hash changed.
8. **Directory summaries** (`services/dir_summaries.py::generate_dir_summaries`) — group files by parent dir, build a compact context, call the summary model, embed the result, upsert into `dir_summaries`.
9. **Startup analysis** (`services/startup_analysis.py` → `analyze_startup_activity`) — after indexing, build a curated context bundle, call `gpt-5.4` with a JSON-schema-constrained completion, persist into `startup_plans`. Idempotent on `repo_url` unless `force=True`.

**Querying.** Vector tables use HNSW halfvec cosine indexes. Lookups are `1 - (embedding <=> query_vec)` cosine similarity, scoped by `repo_url`. The query is embedded with the same `text-embedding-3-large` model via `embed_query` so vectors live in the same space. Exact lookup uses `repo_text_lines` with pg_trgm-backed substring search or case-insensitive Postgres regex, also scoped by `repo_url`.

## Agents

All seven agents are `openai-agents` SDK `Agent` objects on `gpt-5.4` with `model_settings=ModelSettings(max_tokens=16384)`. Each agent's tool set is intentionally narrow so the router can compose them.

### `router_agent` — [agent_defs.py:131](agent_defs.py#L131)
- **Role:** entry point for every user message. Decides which sub-agent to hand off to, or asks the user to disambiguate.
- **Tools:** `ask_user`.
- **Handoffs:** `explorer_agent`, `explainer_agent`, `tracer_agent`, `bootstrap_agent`. Can chain handoffs across one turn.
- **Routing baked into the prompt:** "how do I run this / install / env vars / Docker / dev-server" → `bootstrap_agent`; git-history questions ("what changed recently", commit/blame) → `explainer_agent` (it owns `git_log`); ambiguous queries → `ask_user` first.

### `explorer_agent` — [agent_defs.py:32](agent_defs.py#L32)
- **Role:** find exact things in the codebase — files by name, symbols by definition site.
- **Tools:** `list_files`, `search_code`, `search_indexed`, `read_file`, `ask_user`.
- **Routing rules baked into the prompt:**
  - File lookup → `list_files` with glob `**/<name>`, return paths only (no line numbers).
  - Symbol lookup (function/class/variable/JSX component) → `search_code`, return `file:line` matches.
  - Natural-language functionality lookup that exact-string search would miss → `search_indexed`.
  - Content read → `read_file` with optional line range.
- **Output style:** terse — paths or `file:line` lists, not prose.

### `explainer_agent` — [agent_defs.py:55](agent_defs.py#L55)
- **Role:** synthesise and explain. Answers "how does X work", "what is the architecture of Y", and any git-history question.
- **Tools:** `search_indexed`, `search_dir_summaries`, `list_files`, `read_file`, `git_log`, `ask_user`, `get_startup_plan`.
- **Handoffs:** `tracer_agent` (for exact-string / regex lookups it lacks).
- **Inputs:** receives both the local clone path and the indexed `repo_url` (the GitHub URL) injected into the developer prompt by `agent_turn_activity::prepend_repo_context`. Must use the GitHub URL for `search_indexed` / `search_dir_summaries`, and the local path for `list_files` / `read_file`.
- **Discipline:** never invent paths — must `list_files` or `search_indexed` first; cites file paths and line ranges in the answer.

### `tracer_agent` — [agent_defs.py:16](agent_defs.py#L16)
- **Role:** follow execution paths. Given a `file:line` or symbol, traces what calls it / what it calls.
- **Tools:** `read_file`, `search_code`, `find_references`, `get_dependencies`, `ask_user`.

### `bootstrap_agent` — [agent_defs.py:82](agent_defs.py#L82)
- **Role:** answer "how do I run this", "what env vars do I need", "why is X required" against the persisted startup plan(s). For multi-repo sessions, "how do I run the whole stack" goes here too.
- **Tools:** `get_startup_plan`, `get_app_startup_plan`, `recompute_startup_plan`, `list_files`, `read_file`, `get_dependencies`, `search_indexed`, `ask_user`.
- **Handoffs:** `explainer_agent`, `tracer_agent`.
- **Behaviour:** start every answer by reading the relevant plan. For cross-stack questions on multi-repo sessions → call `get_app_startup_plan(session_id)` (the consolidated markdown). For one-repo questions → `get_startup_plan(repo_url)`. Cite plan step numbers and `file:line` sources from the plan's `sources` arrays. If `analysis_status == 'failed'` (or no plan exists), investigate manifests/Dockerfiles/etc. directly and synthesise a walkthrough — but do **not** auto-call `recompute_startup_plan`; only do so when the user explicitly asks.

### `boundary_extractor_agent` — [agent_defs.py:134](agent_defs.py#L134)
- **Role:** per-repo wire-boundary extraction. Produces a strict `BoundaryReport` ([services/boundary_extractor.py](services/boundary_extractor.py)) describing what HTTP routes the repo exposes, what HTTP/DB endpoints it consumes (with both env-var name AND any resolved value), what dev-server proxies it configures, and what infra services it requires.
- **Output type:** `Agent[BoundaryReport]` — SDK-native structured output via Pydantic.
- **Tools:** `list_files`, `read_file`, `get_dependencies`, `search_code`, `search_indexed`. **No** `ask_user` — ambiguities go in the report's `ambiguities[]` field.
- **Inputs:** local repo path + indexed `repo_url` + the just-produced per-repo startup plan (as prior context, so it doesn't re-derive runtime/env-var info).
- **Run by:** `extract_boundaries_activity`, once per repo, after `analyze_startup_activity`.

### `consolidator_agent` — [agent_defs.py:160](agent_defs.py#L160)
- **Role:** one-shot streamed agent that produces the final, ordered, human-facing markdown for the **app-level** startup plan covering all repos in the session.
- **Tools:** `get_repo_boundaries`, `get_repo_startup_plan`, `list_files`, `read_file`, `search_indexed`. **No** `ask_user` — unresolved questions go in the markdown's Caveats section.
- **Inputs (in developer prompt):** repo list with local paths + indexed URLs, the matcher's full `DependencyGraph` JSON, ambiguities, orchestration findings.
- **Output:** six-section markdown — `# Startup plan: …`, `## Prerequisites`, `## Env vars`, `## Steps` (one numbered step per topo group), `## Dependency graph` (Mermaid), `## Caveats`. Instructed: *don't silently override the matcher's topo order; if you do, explain in Caveats.*
- **Run by:** `consolidate_plan_activity`, streamed via `Runner.run_streamed` (text-deltas + tool-calls flow over SSE just like a chat turn).

### Tool catalogue (`services/tools.py`)

| Tool | Signature | What it does |
|---|---|---|
| `list_files` | `(dir_path, glob="**/*") -> list[str]` | Glob the local clone. |
| `read_file` | `(file_path, start_line=0, end_line=-1) -> str` | Read full file or slice; rejects directories. |
| `search_code` | `(dir_path, query, file_type="") -> list[str]` | Regex over local clone contents → `file:line` results, optionally filtered by extension. |
| `search_exact_indexed` | `(query, repo_url, limit=50, regex=False, path="", language="") -> str` (async) | Exact string or regex over persisted line inventory → `file:line: text` results. |
| `find_references` | `(symbol, dir_path) -> list[str]` | Same regex shape as `search_code`, framed as "where is symbol used". |
| `get_dependencies` | `(file_path) -> list[str]` | Extract imports — `import X from "y"` / `require()` / `import()` for JS, `from X import` / `import X` for Python. |
| `search_indexed` | `(query, repo_url, k=10) -> str` (async) | Embed query + cosine similarity over `code_chunks`, returns top-k formatted blocks `[score] path (type: name) Lstart-end\n<content>`. |
| `search_dir_summaries` | `(query, repo_url, k=5) -> str` (async) | Same pattern over `dir_summaries` for high-level "what's in this folder" answers. |
| `git_log` | `(repo_dir, file_path="", limit=10) -> list[str]` | `git log --pretty=format:%h %s -n <limit>`, optionally scoped to a path. |
| `ask_user` | `(question, options=None) -> str` (async) | **Human-in-the-loop.** Inserts a `pending_actions` row, publishes `data-needs-input` to the SSE stream, returns a placeholder string. The current `session_id` is read from a `contextvars.ContextVar` set by `agent_turn_activity`. |
| `get_startup_plan` | `(repo_url) -> str` (async) | Read the persisted `startup_plans` row for a repo and render it as a structured markdown summary for the LLM (runtime, package manager, env vars w/ required+confidence, services, ordered steps, warnings). Returns `"No startup plan available …"` if missing. |
| `get_repo_startup_plan` | `(repo_url) -> str` (async) | Alias of `get_startup_plan` exposed to the consolidator under the cross-repo naming convention. Same renderer. |
| `get_repo_boundaries` | `(repo_url) -> str` (async) | JSON-dump the persisted `repo_boundaries.report` plus its `analysis_status` + `model`. Used by the consolidator to read each repo's wire boundaries. |
| `get_app_startup_plan` | `(session_id) -> str` (async) | Resolve `sessions.app_plan_hash`, look up `app_startup_plans.plan_markdown`, return the consolidated markdown for the whole session. Used by `bootstrap_agent` for cross-stack questions. |
| `recompute_startup_plan` | `(repo_url, reason="") -> str` (async) | Look up the current session via the `current_session_id` ContextVar, resolve the workflow handle `chat-<session_id>`, and signal `recompute_startup_plan(reason)` to force a re-analysis (which now reruns the **whole** pipeline). |

## Data model (`services/db.py::SCHEMA_SQL`)

| Table | Purpose | Key fields |
|---|---|---|
| `tenants` | Customer/workspace owner for indexed source and learned facts. | `slug`, `name`, `plan`. |
| `repo_connections` | Tenant-scoped repo connection metadata. | `tenant_id`, `provider`, `repo_url`, `default_branch`, `installation_id`, `metadata`. |
| `repo_indexes` / `repo_latest_indexes` | Versioned repo index records and latest serving pointer. | `repo_connection_id`, `manifest_sha256`, `commit_sha`, `branch`, counts, status. |
| `repo_index_jobs` | Durable indexing job state. | `repo_connection_id`, `status`, `attempt_count`, `target_ref`, `metrics`, error fields. |
| `code_chunks` | AST-level chunks + 3072-dim embeddings. HNSW halfvec cosine index. | `repo_url`, `file_path`, `chunk_sha256`, `embedding_sha256`, `chunk_type`, `name`, `parent_class`, `start_line`, `end_line`, `content`, `embedding`. Upserts by `(repo_url, chunk_sha256)` when hashes are available. |
| `dir_summaries` | LLM-summarised directories + embeddings. | `dir_path`, `summary`, `file_list TEXT[]`, `embedding`. Unique on `(repo_url, dir_path)`. |
| `repo_index_runs` | Append-only manifest history. | `repo_url`, `manifest_sha256`, `file_count`, `chunk_count`, `metadata jsonb`, `created_at`. |
| `repo_files` | Latest file inventory. | `repo_url`, `file_path`, `file_sha256`, `size_bytes`, `language`, generated/vendor flags. |
| `repo_text_lines` | Latest non-empty line inventory for exact lookup. | `repo_url`, `file_path`, `file_sha256`, `language`, `line_number`, `line_text`. |
| `repo_chunk_manifests` | Latest chunk inventory. | `repo_url`, `chunk_sha256`, `embedding_sha256`, `file_path`, `file_sha256`, line range, token count. |
| `repo_embedding_cache` | Repo-scoped embedding cache. | `repo_url`, `embedding_sha256`, `embedding_model`, `embedding`, `last_used_at`. |
| `sessions` | One row per chat session. | `id uuid`, `status (indexing\|ready\|ended)`, `app_plan_hash` (FK-style pointer into `app_startup_plans`), `created_at`, `last_seen_at`. |
| `session_repos` | N rows per session — one per repo in the session. | PK `(session_id, repo_url)`. |
| `messages` | Persisted chat transcript (the source of truth for the **frontend**). | `role (user\|assistant\|system\|tool)`, `parts jsonb` (AI-SDK-v6 UI Message parts so the FE renders directly). |
| `pending_actions` | Backs human-in-the-loop pauses. | `kind`, `payload jsonb`, `status (open\|resolved\|cancelled)`, `resolved_value jsonb`, `resolved_at`. |
| `startup_plans` | Per-repo "how to run this" plan produced by `analyze_startup_activity`. | PK `repo_url`, `plan jsonb`, `analysis_status (ok\|partial\|failed)`, `overall_confidence`, `model`, `truncations TEXT[]`, `error`, `created_at`, `updated_at`. |
| `repo_boundaries` | Per-repo wire-boundary report produced by `extract_boundaries_activity`. | PK `repo_url`, `report jsonb` (= `BoundaryReport.model_dump()`), `analysis_status (ok\|partial\|failed)`, `model`, `error`, `created_at`, `updated_at`. |
| `app_startup_plans` | Consolidated app-level plan keyed on the **set of repos**. Cache key = `repo_set_hash = sha256("\n".join(sorted(rstripped repo_urls)))`. | PK `repo_set_hash`, `repo_urls TEXT[]`, `plan_markdown TEXT` (consolidator output), `graph jsonb` (full `DependencyGraph`), `ambiguities jsonb`, `orchestration_findings jsonb`, `analysis_status`, `model`, `error`, `created_at`, `updated_at`. Two sessions over the same repo set share the same row — no recomputation. **Does not** detect upstream repo changes (deferred). |

`init_schema()` runs on FastAPI startup, creates the `vector` extension on a raw connection (the pool's `register_vector_async` configure hook needs the type to exist), then runs the `IF NOT EXISTS` schema.

**Two distinct conversation stores, on purpose:**
- `messages` — what the frontend hydrates from (`GET /sessions/:id/messages`) and renders.
- `SQLiteSession(session_id, AGENT_SESSION_DB)` — the OpenAI Agents SDK's own short-term memory used to feed the next agent turn. Persisted to `agent_sessions.db` by default.

Do not try to unify them; they have different consumers.

## HTTP surface (`main.py`)

**Session product endpoints:**
- `POST /sessions { repo_urls: [...] }` — accepts an array of repo URLs (legacy `{ repo_url }` is still accepted as a one-element list). Normalises (`rstrip("/")`, dedupe, sort), computes `repo_set_hash`, inserts a `sessions` row (`status='indexing'`, `app_plan_hash=<hash>`) and N `session_repos` rows. Starts `CodebaseChatWorkflow` with id `chat-<session_id>` and `ChatParams(session_id, repo_urls, repo_set_hash)`. Returns `{ session_id, repo_urls }`.
- `GET /sessions/:id` — current status.
- `GET /sessions/:id/messages` — full transcript for hydration.
- `POST /sessions/:id/messages { content }` — writes user message, signals the workflow with `user_message`, subscribes to the per-session event bus, returns an SSE `text/event-stream` of agent events until a `finish` event arrives or 300s idle timeout.
- `GET /sessions/:id/startup-plan` — **app-level** consolidated plan. Resolves `sessions.app_plan_hash` → `app_startup_plans` row. Returns `{ repo_set_hash, repo_urls, plan_markdown, graph, ambiguities, orchestration_findings, analysis_status, model, error, updated_at }`. `404 {"status": "pending"}` until the consolidator finishes.
- `POST /sessions/:id/startup-plan/recompute { reason? }` — signals `recompute_startup_plan(reason)`; returns `202 {"status": "recomputing"}`. The workflow reruns the **whole** pipeline (per-repo gather with `force=True` → matcher → consolidator).
- `POST /sessions/:id/startup-plan/export` — thin wrapper around the persisted `app_startup_plans.plan_markdown`: convert to PDF via `services/pdf_output.py::write_markdown_pdf`, return `{ session_id, repo_urls, markdown, pdf_base64 }`. **No agent runs** — the consolidator already verified.

**Per-repo diagnostic endpoints** (`:repo_url` is URL-encoded):
- `GET /sessions/:id/repos/:repo_url/startup-plan` — per-repo `startup_plans` row.
- `GET /sessions/:id/repos/:repo_url/boundaries` — per-repo `repo_boundaries` row.

**Exploratory / debug endpoints (no session, direct one-shot):**
- `GET /walkrepo` — flat tree dump.
- `GET /chunks` — clone + chunk + store, returns chunk metadata + previews.
- `GET /manifest` — clone + chunk without embeddings, persists file/chunk/line inventories when `persist=true`.
- `GET /ast` — tree-sitter AST dump for source files.
- `GET /explore` — one-shot run of `explorer_agent` with `Runner.run` (not streamed).
- `GET /search` — direct cosine search over `code_chunks`. `query` is read raw via `_raw_query_param` so `%`/special chars are kept literal.
- `GET /search-exact` — direct exact string or regex search over `repo_text_lines`; supports `limit`, `regex`, `path`, and `language`.

## Streaming protocol

`agent_turn_activity` runs `Runner.run_streamed(router_agent, …)` with a `RunConfig(session_input_callback=prepend_repo_context)` that prepends a `developer` message containing the local repo dir + indexed `repo_url` + senior-developer system prompt. It walks the SDK event stream and **emits AI-SDK-v6-style UI Message parts** to two places:

1. The per-session asyncio queue (`services/event_bus.publish`) — consumed by the SSE response.
2. The placeholder assistant `messages` row created at the start of the turn (via `parts = parts || %s::jsonb` append) — so reload reproduces the same render.

Event mapping:

| SDK event | Emitted part |
|---|---|
| `RawResponsesStreamEvent` (text delta) | `{ "type": "text-delta", "textDelta": <delta> }` (stream only — final `text` part is appended once). |
| `MessageOutputItem` (`message_output_completed`) | `{ "type": "text", "text": <full> }` — persisted only. |
| `ToolCallItem` (`tool_called`) | `{ "type": "tool-input-available", "toolCallId", "toolName", "args" }`. |
| `ToolCallOutputItem` (`tool_output`) | `{ "type": "tool-output-available", "toolCallId", "output" }`. |
| `HandoffCallItem` / `HandoffOutputItem` / `AgentUpdatedStreamEvent` | `{ "type": "data-handoff", "agent": <name> }`. |
| `ask_user` tool execution | `{ "type": "data-needs-input", "pendingId", "question", "options" }` (published from inside the tool itself). |
| `clone_repo_activity` / `index_repo_activity` / `extract_boundaries_activity` | `{ "type": "data-repo-progress", "repo_url", "stage": "cloning|cloned|indexing|indexed|extracting_boundaries|boundaries_extracted" }` (per-stage, per-repo). |
| `analyze_startup_activity` (post-indexing or recompute) | `{ "type": "data-startup-plan-updated", "updatedAt": <iso> }`. |
| `build_graph_activity` (after matcher) | `{ "type": "data-graph-built", "node_count", "edge_count", "ambiguity_count" }`. |
| `consolidate_plan_activity` (start) | `{ "type": "data-consolidator-started", "repo_set_hash" }` then text-deltas + tool events identical to a chat turn. |
| `consolidate_plan_activity` (end) | `{ "type": "data-app-plan-updated", "updatedAt", "repo_set_hash" }`. |
| End of turn | `{ "type": "finish" }`. |

If the turn ends with an open `pending_actions` row, `agent_turn_activity` returns `{"kind": "paused", "pending_id", "payload"}` and the workflow stashes it in `self._pending` until a follow-up `user_message` (auto-resolves the pending row) or a `clarification_response` signal arrives.

## Temporal

**Worker.** Spun up inside the FastAPI lifespan in `main.py`: `Client.connect(TEMPORAL_HOST)` → `Worker(client, task_queue="onboarding-queue", workflows=[CodebaseChatWorkflow], activities=[…ten activities…])`. The worker runs in-process with the API server, so a uvicorn reload restarts it.

**Workflow id convention.** `chat-<session_id>`. `POST /sessions` starts the workflow; `POST /sessions/:id/messages` resolves the handle by id and signals it.

**Activities & their timeouts** (all on `onboarding-queue`):

| Activity | Params | `start_to_close` | Retry attempts | Purpose |
|---|---|---|---|---|
| `clone_repo_activity` | `CloneParams(repo_url, session_id)` | 120s | 3 | `ensure_repo_dir`; raises if clone fails. Publishes `data-repo-progress`. |
| `index_repo_activity` | `IndexParams(repo_url, repo_dir, session_id)` | 600s | 2 | Walk → AST chunk → manifest → exact line inventory → hydrate embedding cache → embed misses → `store_chunks` → regenerate summaries only when the manifest changed. Publishes `data-repo-progress`. |
| `analyze_startup_activity` | `AnalyzeStartupParams(session_id, repo_url, repo_dir, force=False)` | 120s | 2 | `build_context` → `call_llm` (`gpt-5.4`, JSON-schema-constrained) → `upsert_startup_plan`. Idempotent unless `force=True`; publishes `data-startup-plan-updated` either way. |
| `extract_boundaries_activity` | `ExtractBoundariesParams(session_id, repo_url, repo_dir)` | 240s | 2 | Run `boundary_extractor_agent` (with the per-repo startup plan as prior context) → `BoundaryReport` → `upsert_repo_boundaries`. On agent failure, persists an empty report with `analysis_status='failed'`. |
| `build_graph_activity` | `BuildGraphParams(session_id, repo_set_hash, repo_urls, repo_dirs)` | 60s | 2 | Pure-code matcher. Loads every repo's `repo_boundaries` + `startup_plans`, calls `services/dependency_graph.py::build_graph`, persists into `app_startup_plans` with placeholder `plan_markdown=""` and `analysis_status='partial'`. Publishes `data-graph-built`. |
| `consolidate_plan_activity` | `ConsolidateParams(session_id, repo_set_hash, repo_urls, repo_dirs)` | 600s | 1 | Streamed `Runner.run_streamed(consolidator_agent, …)`. Updates `app_startup_plans.plan_markdown`, sets `analysis_status='ok'`, publishes `data-app-plan-updated`. |
| `update_session_status_activity` | `SessionStatusParams(session_id, status)` | 30s | 3 | `UPDATE sessions SET status, last_seen_at`. |
| `agent_turn_activity` | `AgentTurnParams(session_id, content)` | 300s | 1 | Stream one `Runner.run_streamed(router_agent, …)` turn. Reads `session_repos`, resolves all repo dirs, injects every `(name, local, indexed_url)` triple into the developer prompt. Emits to `event_bus` + appends parts to the placeholder `messages` row. |
| `resolve_pending_actions_activity` | `session_id: str` | 15s | 3 | Mark all `open` pending_actions for the session `resolved`. Called when a user reply is interpreted as the answer to an open clarification. |
| `cancel_pending_actions_activity` | `session_id: str` | 15s | 3 | Mark open pending_actions `cancelled`. Called once on `end_session`. |

**Signals & queries** (`workflows.py`):

| Kind | Name | Effect |
|---|---|---|
| signal | `user_message(content: str)` | Append to internal queue; wakes the wait loop. |
| signal | `clarification_response(pending_id: str, value: dict)` | Stash a clarification result; pops `pending_id` from `self._pending`. |
| signal | `end_session()` | Sets `self._ended` so the loop exits. |
| signal | `recompute_startup_plan(reason: str = "")` | Sets `self._recompute_requested`; the wait loop reruns the **whole pipeline** via `_run_pipeline(force=True)` (per-repo gather → matcher → consolidator) before processing further messages. |
| query  | `get_status() -> str` | One of `starting` / `indexing` / `ready` / `ended`. |
| query  | `get_pending() -> list[dict]` | Currently-open pending payloads. |

**Determinism.** `CodebaseChatWorkflow.run` is the only `@workflow.run`. Every side-effect goes through an activity; all module imports inside the workflow file use `workflow.unsafe.imports_passed_through()` so non-deterministic libraries (psycopg, openai, tree-sitter) never get loaded by the workflow sandbox.

## Workflow lifecycle (`workflows.py`)

1. `update_session_status_activity("indexing")`.
2. `_run_pipeline(force=False)`:
   - `asyncio.gather` over each `repo_url`:
     1. `clone_repo_activity(CloneParams(repo_url, session_id))` → repo dir.
     2. `index_repo_activity(IndexParams(repo_url, repo_dir, session_id))` (manifest/cache-aware incremental path).
     3. `analyze_startup_activity(AnalyzeStartupParams(session_id, repo_url, repo_dir, force))` (skipped fast-path if a `startup_plans` row already exists unless `force`).
     4. `extract_boundaries_activity(ExtractBoundariesParams(session_id, repo_url, repo_dir))`.
   - `build_graph_activity(BuildGraphParams(session_id, repo_set_hash, repo_urls, repo_dirs))` — deterministic matcher.
   - `consolidate_plan_activity(ConsolidateParams(session_id, repo_set_hash, repo_urls, repo_dirs))` — streamed consolidator.
3. `update_session_status_activity("ready")`.
4. **Wait loop:** `workflow.wait_condition(self._user_messages or self._clarifications or self._recompute_requested or self._ended)`.
   - On `recompute_startup_plan`: flip status back to `indexing`, call `_run_pipeline(force=True)`, flip back to `ready`.
   - On `user_message`: if a pending action is open, clear `self._pending` and call `resolve_pending_actions_activity`, then run `agent_turn_activity`. If the result is `{"kind": "paused"}`, stash `self._pending[pending_id] = payload`.
5. On `end_session`: `cancel_pending_actions_activity` → `update_session_status_activity("ended")`.

## Conventions

- **No comments** unless the *why* is non-obvious. Names should carry the meaning.
- **Activity params are dataclasses** so Temporal can (de)serialise cleanly.
- **Workflow determinism** — no I/O or wall-clock logic inside `@workflow.run`; everything is an activity.
- **Idempotency at boundaries** — `ensure_repo_dir` short-circuits if the clone exists; `index_repo_activity` hashes the current repo state, reuses cached embeddings, and skips summary regeneration when the manifest is unchanged; all schema uses `IF NOT EXISTS`; unique constraints back upserts.
- **`repo_url` is always `.rstrip('/')`-normalised** at the HTTP boundary before touching the DB.
- **Session id propagation** — `agent_turn_activity` sets `current_session_id` (a `ContextVar`) so deep-down tools like `ask_user` can locate their session without the agent having to pass it explicitly.

## Running locally

```sh
docker compose up -d
# fastapi     → http://localhost:8000
# temporal-ui → http://localhost:8080
# postgres    → localhost:5432  (postgres / postgres / codebase_agent)
```

Required env: `OPENAI_API_KEY`. Optional: `TEMPORAL_HOST`, `DATABASE_URL`, `AGENT_SESSION_DB`, `STARTUP_ANALYSIS_MODEL` (default `gpt-5.4`), `GITHUB_TOKEN` (for private clones).

FastAPI reloads on file change (`--reload`); Temporal workflows pick up code changes when the worker restarts (i.e. the uvicorn restart restarts the worker too).

## Testing

Whenever a new API route is created or an existing one is changed, verify it with `curl` against the test repositories:

```
https://github.com/ThomasBenjaminCook/WattAppWebApp        # single small repo
https://github.com/sindresorhus/p-map                     # tiny utility, indexes fast
https://github.com/HiHobbes/hobbesBackend.git             # large multi-repo (private — needs GITHUB_TOKEN)
https://github.com/HiHobbes/hobbesPlatform.git            # paired with hobbesBackend
```

Multi-repo example flow:

```sh
# 1. Create a multi-repo session
SID=$(curl -s -X POST http://localhost:8000/sessions \
  -H "Content-Type: application/json" \
  -d '{"repo_urls":["https://github.com/ThomasBenjaminCook/WattAppWebApp",
                    "https://github.com/sindresorhus/p-map"]}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')

# 2. Poll status until 'ready'  (clone + index + analyze + extract + matcher + consolidator)
curl -s http://localhost:8000/sessions/$SID

# 3. Read the consolidated app-level startup plan (markdown + graph + ambiguities)
curl -s http://localhost:8000/sessions/$SID/startup-plan

# 4. Per-repo diagnostics (URL-encoded :repo_url)
curl -s "http://localhost:8000/sessions/$SID/repos/https%3A%2F%2Fgithub.com%2Fsindresorhus%2Fp-map/startup-plan"
curl -s "http://localhost:8000/sessions/$SID/repos/https%3A%2F%2Fgithub.com%2Fsindresorhus%2Fp-map/boundaries"

# 5. Send a chat message (ambiguous between repos → expect ask_user)
curl -N -X POST http://localhost:8000/sessions/$SID/messages \
  -H "Content-Type: application/json" \
  -d '{"content":"where is the entry point?"}'

# 6. Cross-stack chat → router → bootstrap → get_app_startup_plan
curl -N -X POST http://localhost:8000/sessions/$SID/messages \
  -H "Content-Type: application/json" \
  -d '{"content":"How do I run the whole stack locally?"}'

# 7. Force a full re-analysis (whole pipeline reruns with force=True)
curl -s -X POST http://localhost:8000/sessions/$SID/startup-plan/recompute \
  -H "Content-Type: application/json" \
  -d '{"reason":"refresh"}'

# 8. Export markdown + PDF (instant — no agent runs)
curl -s -X POST http://localhost:8000/sessions/$SID/startup-plan/export

# 9. Error cases
curl -s -X POST http://localhost:8000/sessions -H "Content-Type: application/json" -d '{}'
curl -s http://localhost:8000/sessions/00000000-0000-0000-0000-000000000000
```

Legacy single-repo `{"repo_url": "..."}` payload is still accepted on `POST /sessions` for backward compatibility (treated as a one-element list).

Run the matcher unit tests with: `python3 scripts/test_dependency_graph.py`.

Always test the happy path, expected error responses, and state transitions (`indexing` → `ready` → `ended`).

## Roadmap (phased)

- **Phase 3 — long-lived chat workflow** ✅
- **Phase 4 — streaming agent turn** ✅ (`Runner.run_streamed` → AI SDK v6 parts → per-event persistence)
- **Phase 5 — human-in-the-loop** ✅ (`ask_user`, `pending_actions`, `clarification_response`)
- **Phase 6 — SSE endpoint** ✅
- **Phase 7 — startup analysis** ✅ (`analyze_startup_activity`, `startup_plans`, `bootstrap_agent`, `recompute_startup_plan` signal, `GET/POST .../startup-plan(/recompute|/export)`).
- **Phase 8 — multi-repo sessions** ✅ (`session_repos`, `repo_boundaries`, `app_startup_plans`, `boundary_extractor_agent`, `consolidator_agent`, deterministic `services/dependency_graph.py` matcher; per-repo + cross-repo workflow split; multi-repo chat; whole-pipeline recompute; per-repo diagnostic endpoints). See [docs/multi_repo_startup_plan.md](docs/multi_repo_startup_plan.md) for the design.
- **Phase 9 — cleanup:** TTL session sweeper, structured logging, rate limiting, swap `SQLiteSession` for `SQLAlchemySession` for multi-replica deployment. Plus deferred multi-repo extensions: parent-dir-spanning orchestration (one umbrella `docker-compose.yml` referencing multiple repos as subdirs), monorepo workspace expansion (`pnpm-workspace.yaml`/`turbo.json` packages as logical units), staleness detection on `app_startup_plans` when an upstream `repo_boundaries` changes, additional boundary kinds (`graphql`, `websocket`, `shared_types`), and per-repo `recompute` endpoints.

## Deprecated

- `GET /askQuestion`, `CodebaseOnboardingWorkflow`, `WorkflowParams`, `AskParams`, `ask_agent_activity` — replaced by the session-based chat flow.
