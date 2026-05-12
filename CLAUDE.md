# codebase-onboarding-agent

Backend API for long-lived AI chat sessions over any GitHub repository. Point the agent at a repo URL: it clones, AST-chunks, embeds, and summarises the codebase, then answers questions through a router-of-agents loop that streams `file:line` citations to the frontend over SSE. This repo is the backend only.

## Core flow at a glance

```
HTTP (FastAPI)        Temporal workflow            Activities                 Agents (OpenAI Agents SDK)
─────────────         ────────────────────         ──────────────────         ─────────────────────────
main.py               CodebaseChatWorkflow         clone_repo_activity        router_agent
  POST /sessions  ──▶   indexing → ready ──▶         ensure_repo_dir            ├── explorer_agent
  POST /…/messages ─signal▶ user_message              index_repo_activity        ├── explainer_agent
  GET  /…/messages       wait_condition loop          chunk_file_list +          ├── tracer_agent
  POST /…/startup-plan/recompute                       generate_dir_summaries    └── bootstrap_agent
  POST /…/startup-plan/export                        analyze_startup_activity
  GET  /…/startup-plan                                 build_context + call_llm
  SSE stream  ◀──── event_bus.publish                                          Runner.run_streamed
                                                    agent_turn_activity ──────▶ + SQLiteSession
                                                    cancel/resolve_pending_actions
```

One **Temporal workflow per session**, one **chat thread per workflow**, one **repo per session**. The workflow owns the session lifecycle (`indexing` → `ready` → `ended`), produces a persisted `startup_plans` row during indexing via `analyze_startup_activity`, and routes every user message to a single `agent_turn_activity` that streams events to a per-session pubsub which the FastAPI SSE endpoint forwards to the client.

A planned multi-repo extension (one session over N repos with a consolidated app-level plan) is fully designed but **not yet implemented** — see [docs/multi_repo_startup_plan.md](docs/multi_repo_startup_plan.md). Anything below describes the current single-repo behaviour.

## Repo layout

| Path | Role |
|---|---|
| `main.py` | FastAPI app. Owns lifespan (init Postgres schema, start Temporal worker on task queue `onboarding-queue`), HTTP endpoints, SSE streaming. |
| `workflows.py` | `CodebaseChatWorkflow` — durable per-session orchestrator. Signals: `user_message`, `clarification_response`, `end_session`, `recompute_startup_plan`. Queries: `get_status`, `get_pending`. |
| `activities.py` | All seven Temporal activities (`clone_repo_activity`, `index_repo_activity`, `analyze_startup_activity`, `update_session_status_activity`, `agent_turn_activity`, `cancel_pending_actions_activity`, `resolve_pending_actions_activity`) + dataclass params (`IndexParams`, `ChatParams`, `AgentTurnParams`, `SessionStatusParams`, `AnalyzeStartupParams`). Streaming agent turn lives here. |
| `agent_defs.py` | The five agents (`router`, `explorer`, `explainer`, `tracer`, `bootstrap`) with their tool sets, prompts, handoffs. All on `gpt-5.4` with `max_tokens=16384`. |
| `services/clone_repo.py` | `git clone` with optional `GITHUB_TOKEN` injection; idempotent `ensure_repo_dir`. |
| `services/walk_repo.py` | Filtered file iteration (skips `node_modules`, `.git`, build dirs; keeps source/markup/docs extensions). |
| `services/chunk_and_embed.py` | Tree-sitter AST chunking for Python/JS/TS/TSX, markdown heading split, whole-file fallback for config/HTML/CSS/shell. Token-budget splitter + OpenAI batch embedding. |
| `services/dir_summaries.py` | LLM-generated (`gpt-5.4`) per-directory natural-language summaries, then embedded for high-level semantic browsing. |
| `services/db.py` | Postgres + pgvector pool, schema bootstrap, upserts for `code_chunks`, `dir_summaries`, and `startup_plans`. |
| `services/startup_analysis.py` | Builds a curated context bundle (`README`, manifests, env templates, infra/CI files, top-level skeleton; budget-capped at ~32k chars), calls OpenAI (`STARTUP_ANALYSIS_MODEL`, default `gpt-5.4`) with a JSON-schema-constrained response → produces the `startup_plans.plan` JSON. |
| `services/pdf_output.py` | `write_markdown_pdf` — converts the verified startup plan markdown to PDF (used by `POST /sessions/:id/startup-plan/export`). |
| `services/tools.py` | Function tools exposed to the agents (file/code search, semantic search, references, deps, git log, `ask_user`, `get_startup_plan`, `recompute_startup_plan`). Owns the `current_session_id` ContextVar. |
| `services/event_bus.py` | In-process per-session asyncio `Queue` fan-out used to bridge the activity → SSE response. |
| `scripts/` | Manual debugging helpers (`show_chunks.py`, `show_ast.py`, `test_ask_question.py`, `test_startup_plan.py`). |

## Vector indexing pipeline

All embeddings — for chunks, directory summaries, and live queries — use OpenAI `text-embedding-3-large` (3072-dim, stored as pgvector `halfvec(3072)`). Tokenisation budget is enforced with `tiktoken.encoding_for_model("text-embedding-3-large")`. The 7500-token cap on `embedding_text` leaves headroom under the model's 8191-token limit.

Indexing happens once per repo, on first session creation, inside `index_repo_activity`. It is **idempotent**: if `code_chunks` already has rows for the `repo_url`, the activity returns early.

1. **Clone** (`services/clone_repo.py::ensure_repo_dir`) — clones into `/repos/<repo_name>` if not already present; reuses the directory across sessions of the same repo. Tokens are stripped from any error logs.
2. **Walk** (`services/walk_repo.py::collect_file_paths`) — depth-first walk skipping vendored/build dirs (`node_modules`, `.next`, `dist`, …) and keeping source + markup + docs extensions only.
3. **AST chunk** (`services/chunk_and_embed.py::chunk_file_list` → `_dispatch_extract`) — extension-dispatched semantic chunking:
   - **Python** (`extract_chunks_from_file`): module docstring, grouped imports block, top-level functions (incl. decorated), classes. Classes <60 lines stay whole; larger classes split into a header chunk + one chunk per method (with `parent_class` set).
   - **JS/JSX/TS/TSX** (`extract_js_chunks`): top-level functions, classes, interfaces, type aliases, enums, lexical/variable declarations. `export` wrappers are unwrapped to inspect the inner declaration. Imports are grouped into one chunk. Files with no extractable top-level decls (mostly JSX) fall back to a single whole-file chunk.
   - **Markdown** (`extract_markdown_chunks`): split on `#`/`##`/`###` headings into named sections.
   - **JSON / YAML / CSS / HTML / shell** (`extract_whole_file_chunk`): treated as one chunk per file with a typed label (`config`, `stylesheet`, `markup`, `shell`).
4. **Token-budget split** (`split_oversized`) — any chunk whose `embedding_text` exceeds `MAX_TOKENS = 7500` is recut at line boundaries with `OVERLAP_LINES = 5` overlap, preserving `chunk_type`, `name`, `parent_class` and recording `part N` in the name.
5. **Prefix + embed** — each chunk's `embedding_text` is `"# File: <path>\n# Class: <parent>\n# <chunk_type>: <name>\n<content>"`. Chunks are batched into `client.embeddings.create(model="text-embedding-3-large")` → 3072-dim vectors.
6. **Upsert** (`services/db.py::store_chunks`) — `INSERT … ON CONFLICT ON CONSTRAINT code_chunks_unique` on `(repo_url, file_path, start_line, chunk_type, name)` (NULLS NOT DISTINCT) so re-indexing rewrites in place.
7. **Directory summaries** (`services/dir_summaries.py::generate_dir_summaries`) — group files by parent dir, build a compact context (file list + first 40 lines of each, capped at 12k chars), call `gpt-5.4` with a senior-engineer summarisation prompt, embed the result, upsert into `dir_summaries`.
8. **Startup analysis** (`services/startup_analysis.py` → `analyze_startup_activity`) — separately, after indexing, build a curated context bundle (`README`, manifests, env templates, infra/CI files, top-level skeleton), call `gpt-5.4` with a JSON-schema-constrained completion, persist into `startup_plans`. Idempotent on `repo_url` unless `force=True`.

**Querying.** Both vector tables use `hnsw (embedding halfvec_cosine_ops)`. Lookups are `1 - (embedding <=> query_vec::halfvec)` cosine similarity, scoped by `repo_url`. The query is embedded with the same `text-embedding-3-large` model via `embed_query` so vectors live in the same space.

## Agents

All five agents are `openai-agents` SDK `Agent` objects on `gpt-5.4` with `model_settings=ModelSettings(max_tokens=16384)`. Each agent's tool set is intentionally narrow so the router can compose them.

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

### `bootstrap_agent` — [agent_defs.py:79](agent_defs.py#L79)
- **Role:** answer "how do I run this", "what env vars do I need", "why is X required" against the persisted startup plan. The plan is the source of truth.
- **Tools:** `get_startup_plan`, `recompute_startup_plan`, `list_files`, `read_file`, `get_dependencies`, `search_indexed`, `ask_user`.
- **Handoffs:** `explainer_agent`, `tracer_agent`.
- **Behaviour:** start every answer by reading the plan; cite plan step numbers and `file:line` sources from the plan's `sources` arrays. If `analysis_status == 'failed'` (or no plan exists), investigate manifests/Dockerfiles/etc. directly and synthesise a walkthrough — but do **not** auto-call `recompute_startup_plan`; only do so when the user explicitly asks. Also reused by `POST /sessions/:id/startup-plan/export` to verify and reformat the plan into a polished markdown PDF.

### Tool catalogue (`services/tools.py`)

| Tool | Signature | What it does |
|---|---|---|
| `list_files` | `(dir_path, glob="**/*") -> list[str]` | Glob the local clone. |
| `read_file` | `(file_path, start_line=0, end_line=-1) -> str` | Read full file or slice; rejects directories. |
| `search_code` | `(dir_path, query, file_type="") -> list[str]` | Regex over file contents → `file:line` results, optionally filtered by extension. |
| `find_references` | `(symbol, dir_path) -> list[str]` | Same regex shape as `search_code`, framed as "where is symbol used". |
| `get_dependencies` | `(file_path) -> list[str]` | Extract imports — `import X from "y"` / `require()` / `import()` for JS, `from X import` / `import X` for Python. |
| `search_indexed` | `(query, repo_url, k=10) -> str` (async) | Embed query + cosine similarity over `code_chunks`, returns top-k formatted blocks `[score] path (type: name) Lstart-end\n<content>`. |
| `search_dir_summaries` | `(query, repo_url, k=5) -> str` (async) | Same pattern over `dir_summaries` for high-level "what's in this folder" answers. |
| `git_log` | `(repo_dir, file_path="", limit=10) -> list[str]` | `git log --pretty=format:%h %s -n <limit>`, optionally scoped to a path. |
| `ask_user` | `(question, options=None) -> str` (async) | **Human-in-the-loop.** Inserts a `pending_actions` row, publishes `data-needs-input` to the SSE stream, returns a placeholder string. The current `session_id` is read from a `contextvars.ContextVar` set by `agent_turn_activity`. |
| `get_startup_plan` | `(repo_url) -> str` (async) | Read the persisted `startup_plans` row for a repo and render it as a structured markdown summary for the LLM (runtime, package manager, env vars w/ required+confidence, services, ordered steps, warnings). Returns `"No startup plan available …"` if missing. |
| `recompute_startup_plan` | `(repo_url, reason="") -> str` (async) | Look up the current session via the `current_session_id` ContextVar, resolve the workflow handle `chat-<session_id>`, and signal `recompute_startup_plan(reason)` to force a re-analysis. |

## Data model (`services/db.py::SCHEMA_SQL`)

| Table | Purpose | Key fields |
|---|---|---|
| `code_chunks` | AST-level chunks + 3072-dim `halfvec` embeddings, hnsw cosine index. | `repo_url`, `file_path`, `chunk_type`, `name`, `parent_class`, `start_line`, `end_line`, `content`, `embedding halfvec(3072)`. Unique on `(repo_url, file_path, start_line, chunk_type, name)` NULLS NOT DISTINCT. |
| `dir_summaries` | LLM-summarised directories + 3072-dim `halfvec` embeddings, hnsw cosine index. | `dir_path`, `summary`, `file_list TEXT[]`, `embedding halfvec(3072)`. Unique on `(repo_url, dir_path)`. |
| `sessions` | One row per chat session. | `id uuid`, `repo_url`, `status (indexing\|ready\|ended)`, `created_at`, `last_seen_at`. |
| `messages` | Persisted chat transcript (the source of truth for the **frontend**). | `role (user\|assistant\|system\|tool)`, `parts jsonb` (AI-SDK-v6 UI Message parts so the FE renders directly). |
| `pending_actions` | Backs human-in-the-loop pauses. | `kind`, `payload jsonb`, `status (open\|resolved\|cancelled)`, `resolved_value jsonb`, `resolved_at`. |
| `startup_plans` | Per-repo "how to run this" plan produced by `analyze_startup_activity`. | PK `repo_url`, `plan jsonb`, `analysis_status (ok\|partial\|failed)`, `overall_confidence`, `model`, `truncations TEXT[]`, `error`, `created_at`, `updated_at`. |

`init_schema()` runs on FastAPI startup, creates the `vector` extension on a raw connection (the pool's `register_vector_async` configure hook needs the type to exist), then runs the `IF NOT EXISTS` schema.

**Two distinct conversation stores, on purpose:**
- `messages` — what the frontend hydrates from (`GET /sessions/:id/messages`) and renders.
- `SQLiteSession(session_id, AGENT_SESSION_DB)` — the OpenAI Agents SDK's own short-term memory used to feed the next agent turn. Persisted to `agent_sessions.db` by default.

Do not try to unify them; they have different consumers.

## HTTP surface (`main.py`)

**Session product endpoints:**
- `POST /sessions { repo_url }` — inserts a row (`status='indexing'`), starts `CodebaseChatWorkflow` with id `chat-<session_id>`, returns `{ session_id }`.
- `GET /sessions/:id` — current status.
- `GET /sessions/:id/messages` — full transcript for hydration.
- `POST /sessions/:id/messages { content }` — writes user message, signals the workflow with `user_message`, subscribes to the per-session event bus, returns an SSE `text/event-stream` of agent events until a `finish` event arrives or 300s idle timeout.
- `GET /sessions/:id/startup-plan` — returns the persisted `startup_plans` row for this session's repo, or `404 {"status": "pending"}` if `analyze_startup_activity` hasn't completed yet. Shape: `{ repo_url, plan, analysis_status, overall_confidence, model, truncations, error, updated_at }`.
- `POST /sessions/:id/startup-plan/recompute { reason? }` — signals the workflow with `recompute_startup_plan(reason)`; returns `202 {"status": "recomputing"}`. The workflow re-runs `analyze_startup_activity` with `force=True` and publishes `data-startup-plan-updated` when done.
- `POST /sessions/:id/startup-plan/export` — runs `bootstrap_agent` to verify the persisted plan against the actual repo, render a polished single-document markdown ("# Startup plan", env vars, steps, runtime, services, external tools, notes), convert to PDF via `services/pdf_output.py::write_markdown_pdf`, and return `{ session_id, repo_url, markdown, pdf_base64 }`. The agent is told **not** to call `recompute_startup_plan` and to omit verification narrative from the output.

**Exploratory / debug endpoints (no session, direct one-shot):**
- `GET /walkrepo` — flat tree dump.
- `GET /chunks` — clone + chunk + store, returns chunk metadata + previews.
- `GET /ast` — tree-sitter AST dump for source files.
- `GET /explore` — one-shot run of `explorer_agent` with `Runner.run` (not streamed).
- `GET /search` — direct cosine search over `code_chunks`. `query` is read raw via `_raw_query_param` so `%`/special chars are kept literal.

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
| `analyze_startup_activity` (post-indexing or recompute) | `{ "type": "data-startup-plan-updated", "updatedAt": <iso> }` (published to the per-session bus regardless of whether the activity skipped or recomputed). |
| End of turn | `{ "type": "finish" }`. |

If the turn ends with an open `pending_actions` row, `agent_turn_activity` returns `{"kind": "paused", "pending_id", "payload"}` and the workflow stashes it in `self._pending` until a follow-up `user_message` (auto-resolves the pending row) or a `clarification_response` signal arrives.

## Temporal

**Worker.** Spun up inside the FastAPI lifespan in `main.py`: `Client.connect(TEMPORAL_HOST)` → `Worker(client, task_queue="onboarding-queue", workflows=[CodebaseChatWorkflow], activities=[…seven activities…])`. The worker runs in-process with the API server, so a uvicorn reload restarts it.

**Workflow id convention.** `chat-<session_id>`. `POST /sessions` starts the workflow; `POST /sessions/:id/messages` resolves the handle by id and signals it.

**Activities & their timeouts** (all on `onboarding-queue`):

| Activity | Params | `start_to_close` | Retry attempts | Purpose |
|---|---|---|---|---|
| `clone_repo_activity` | `repo_url: str` | 120s | 3 | `ensure_repo_dir`; raises if clone fails. |
| `index_repo_activity` | `IndexParams(repo_url, repo_dir)` | 600s | 2 | Walk → AST chunk → embed → `store_chunks` → `generate_dir_summaries` → `store_dir_summaries`. Idempotent: returns early if `code_chunks` already has rows for `repo_url`. |
| `analyze_startup_activity` | `AnalyzeStartupParams(session_id, repo_url, repo_dir, force=False)` | 120s | 2 | `build_context` → `call_llm` (`gpt-5.4`, JSON-schema-constrained) → `upsert_startup_plan`. Idempotent unless `force=True`; publishes `data-startup-plan-updated` either way. JSON parse fails are retried once before storing `analysis_status='failed'`. |
| `update_session_status_activity` | `SessionStatusParams(session_id, status)` | 30s | 3 | `UPDATE sessions SET status, last_seen_at`. |
| `agent_turn_activity` | `AgentTurnParams(session_id, content)` | 300s | 1 | Stream one `Runner.run_streamed(router_agent, …)` turn; emits to `event_bus` + appends parts to the placeholder `messages` row. |
| `resolve_pending_actions_activity` | `session_id: str` | 15s | 3 | Mark all `open` pending_actions for the session `resolved`. Called when a user reply is interpreted as the answer to an open clarification. |
| `cancel_pending_actions_activity` | `session_id: str` | 15s | 3 | Mark open pending_actions `cancelled`. Called once on `end_session`. |

**Signals & queries** (`workflows.py`):

| Kind | Name | Effect |
|---|---|---|
| signal | `user_message(content: str)` | Append to internal queue; wakes the wait loop. |
| signal | `clarification_response(pending_id: str, value: dict)` | Stash a clarification result; pops `pending_id` from `self._pending`. |
| signal | `end_session()` | Sets `self._ended` so the loop exits. |
| signal | `recompute_startup_plan(reason: str = "")` | Sets `self._recompute_requested`; the wait loop reruns `analyze_startup_activity` with `force=True` before processing further messages. |
| query  | `get_status() -> str` | One of `starting` / `indexing` / `ready` / `ended`. |
| query  | `get_pending() -> list[dict]` | Currently-open pending payloads. |

**Determinism.** `CodebaseChatWorkflow.run` is the only `@workflow.run`. Every side-effect goes through an activity; all module imports inside the workflow file use `workflow.unsafe.imports_passed_through()` so non-deterministic libraries (psycopg, openai, tree-sitter) never get loaded by the workflow sandbox.

## Workflow lifecycle (`workflows.py`)

1. `update_session_status_activity("indexing")`.
2. `clone_repo_activity(repo_url)` → repo dir.
3. `index_repo_activity(repo_url, repo_dir)` (skipped fast-path if already indexed).
4. `analyze_startup_activity(session_id, repo_url, repo_dir, force=False)` (skipped fast-path if a `startup_plans` row already exists; emits `data-startup-plan-updated` either way).
5. `update_session_status_activity("ready")`.
6. **Wait loop:** `workflow.wait_condition(self._user_messages or self._clarifications or self._recompute_requested or self._ended)`.
   - On `recompute_startup_plan`: clear the flag and rerun `analyze_startup_activity` with `force=True`.
   - On `user_message`: if a pending action is open, clear `self._pending` and call `resolve_pending_actions_activity`, then run `agent_turn_activity`. If the result is `{"kind": "paused"}`, stash `self._pending[pending_id] = payload`.
7. On `end_session`: `cancel_pending_actions_activity` → `update_session_status_activity("ended")`.

## Conventions

- **No comments** unless the *why* is non-obvious. Names should carry the meaning.
- **Activity params are dataclasses** so Temporal can (de)serialise cleanly.
- **Workflow determinism** — no I/O or wall-clock logic inside `@workflow.run`; everything is an activity.
- **Idempotency at boundaries** — `ensure_repo_dir` short-circuits if the clone exists; `index_repo_activity` skips if `code_chunks` already has rows for the `repo_url`; all schema uses `IF NOT EXISTS`; unique constraints back upserts.
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

Whenever a new API route is created or an existing one is changed, verify it with `curl` against the test repository:

```
https://github.com/ThomasBenjaminCook/WattAppWebApp
```

Example flow:

```sh
# 1. Create a session
curl -s -X POST http://localhost:8000/sessions \
  -H "Content-Type: application/json" \
  -d '{"repo_url":"https://github.com/ThomasBenjaminCook/WattAppWebApp"}'

# 2. Poll status until 'ready'
curl -s http://localhost:8000/sessions/<session_id>

# 3. Send a message and consume the SSE stream
curl -N -X POST http://localhost:8000/sessions/<session_id>/messages \
  -H "Content-Type: application/json" \
  -d '{"content":"where is the entry point?"}'

# 4. Hydrate the transcript
curl -s http://localhost:8000/sessions/<session_id>/messages

# 5. Read the persisted startup plan
curl -s http://localhost:8000/sessions/<session_id>/startup-plan

# 6. Force a re-analysis
curl -s -X POST http://localhost:8000/sessions/<session_id>/startup-plan/recompute \
  -H "Content-Type: application/json" \
  -d '{"reason":"added a new env var"}'

# 7. Export verified plan + PDF (markdown returned in JSON, PDF as base64)
curl -s -X POST http://localhost:8000/sessions/<session_id>/startup-plan/export

# 8. Error cases
curl -s -X POST http://localhost:8000/sessions -H "Content-Type: application/json" -d '{}'
curl -s http://localhost:8000/sessions/00000000-0000-0000-0000-000000000000
```

Always test the happy path, expected error responses, and state transitions (`indexing` → `ready` → `ended`).

## Roadmap (phased)

- **Phase 3 — long-lived chat workflow** ✅
- **Phase 4 — streaming agent turn** ✅ (`Runner.run_streamed` → AI SDK v6 parts → per-event persistence)
- **Phase 5 — human-in-the-loop** ✅ (`ask_user`, `pending_actions`, `clarification_response`)
- **Phase 6 — SSE endpoint** ✅
- **Phase 7 — startup analysis** ✅ (`analyze_startup_activity`, `startup_plans`, `bootstrap_agent`, `recompute_startup_plan` signal, `GET/POST .../startup-plan(/recompute|/export)`).
- **Phase 8 — multi-repo sessions:** designed in [docs/multi_repo_startup_plan.md](docs/multi_repo_startup_plan.md). Adds `session_repos`, `repo_boundaries`, `app_startup_plans`, a `boundary_extractor_agent` and `consolidator_agent`, and a deterministic `services/dependency_graph.py` matcher. Not yet implemented.
- **Phase 9 — cleanup:** TTL session sweeper, structured logging, rate limiting, swap `SQLiteSession` for `SQLAlchemySession` for multi-replica deployment.

## Deprecated

- `GET /askQuestion`, `CodebaseOnboardingWorkflow`, `WorkflowParams`, `AskParams`, `ask_agent_activity` — replaced by the session-based chat flow.
