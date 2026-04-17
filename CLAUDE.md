# codebase-onboarding-agent

Backend API for long-lived AI chat sessions over any GitHub repository. Point the agent at a repo URL, it clones + chunks + embeds the codebase, then answers questions with `file:line` citations. A separate frontend client consumes this API — this repo is the backend only.

## Purpose

Give a developer (or LLM) a stateful conversational interface for exploring an unfamiliar codebase. Each user session is a single durable Temporal workflow that owns one chat thread against one repository. The agent is multi-hop: it can search, read, trace references, and hand off between specialised sub-agents.

## Architecture

```
HTTP (FastAPI)  ──▶  Temporal workflow (one per session)  ──▶  Activities
  main.py                 CodebaseChatWorkflow                   activities.py
                                                                   ├─ clone_repo_activity
                                                                   ├─ index_repo_activity
                                                                   ├─ agent_turn_activity   ──▶  OpenAI Agents SDK
                                                                   └─ update_session_status_activity
```

- **FastAPI** (`main.py`) — HTTP surface. Non-session endpoints (`/chunks`, `/ast`, `/explore`, `/search`, `/walkrepo`) are exploratory debug tools; session endpoints (`/sessions`, `/sessions/:id`, `/sessions/:id/messages`) are the product.
- **Temporal** (`workflows.py`, `activities.py`) — `CodebaseChatWorkflow` is long-running, one per session. Clone + index happen once at start (idempotent); the workflow then blocks on `workflow.wait_condition` and reacts to signals. Signals: `user_message`, `clarification_response`, `end_session`. Queries: `get_status`, `get_pending`.
- **Agents** (`agent_defs.py`) — `router_agent` hands off to `explorer_agent` (find files/symbols), `explainer_agent` (synthesize/explain with semantic search), or `tracer_agent` (follow execution paths). All use the `openai-agents` SDK.
- **Services** (`services/`) — `clone_repo` (git clone), `walk_repo` (file iteration), `chunk_and_embed` (tree-sitter AST chunking + OpenAI embeddings), `dir_summaries` (per-dir LLM summaries), `tools` (agent function tools), `db` (Postgres + pgvector).
- **Storage** — Postgres with pgvector for all persistent state. SQLite (via `SQLiteSession`) for the Agents SDK's own conversation memory (planned 3.4, separate from the user-facing messages table).

## Data model (`services/db.py::SCHEMA_SQL`)

| Table | Purpose |
|---|---|
| `code_chunks` | AST-level chunks + 1536-dim embeddings (ivfflat). Unique on `(repo_url, file_path, start_line, chunk_type, name)` so re-indexing is idempotent. |
| `dir_summaries` | Per-directory LLM-generated summaries + embeddings. |
| `sessions` | `id uuid`, `repo_url`, `status (indexing|ready|ended)`, `created_at`, `last_seen_at`. |
| `messages` | `id`, `session_id`, `role (user|assistant|system|tool)`, `parts jsonb`, `created_at`. `parts` is AI-SDK-v6-style so the frontend can render directly. |
| `pending_actions` | `id`, `session_id`, `kind`, `payload jsonb`, `status (open|resolved|cancelled)`, `resolved_value jsonb`. Backs human-in-the-loop pauses. |

`init_schema()` runs on app startup and is idempotent (`CREATE ... IF NOT EXISTS`).

## Key flows

**Session creation** — `POST /sessions { repo_url }` inserts a row (`status='indexing'`), starts `CodebaseChatWorkflow` with `id=f"chat-{session_id}"`, returns the UUID. Workflow clones, indexes (skipped if chunks exist), flips to `'ready'`, then waits.

**Chat turn (target shape)** — `POST /sessions/:id/messages { content }` writes the user message to `messages`, signals the workflow, and returns an SSE stream of AI SDK v6 UI Message parts produced by `Runner.run_streamed`. Workflow delegates each turn to `agent_turn_activity(session_id, content)` which uses the Agents SDK's own session memory (`SQLiteSession(session_id, ...)`) as the source of truth for agent context.

**Hydration** — `GET /sessions/:id/messages` returns the full `messages[]` array for frontend reload.

**Clarification** — agent can call `ask_user(...)` tool → inserts a `pending_actions` row → emits a `data-needs-input` stream part → awaits a `clarification_response` signal via `workflow.wait_condition` → resumes with the returned value.

## Conventions

- **No comments** unless explaining a non-obvious *why*. Self-documenting names preferred.
- **Activity params are dataclasses** (`IndexParams`, `ChatParams`, etc.) so Temporal can (de)serialize cleanly.
- **Workflow determinism** — no I/O or time-based logic inside `@workflow.run`; everything goes through activities. Shared imports use `workflow.unsafe.imports_passed_through()`.
- **Idempotency at boundaries** — `ensure_repo_dir` short-circuits if the clone exists; `index_repo_activity` skips if `code_chunks` already has rows for that `repo_url`; all schema objects use `IF NOT EXISTS`; unique constraints enforce upsert semantics.
- **Two sources of truth for conversation**, each with a distinct consumer: the `messages` table (frontend hydration, AI-SDK parts) and the Agents SDK session (agent context retrieval). Do not try to unify.
- `repo_url` is always `.rstrip('/')`-normalised at the HTTP boundary before touching the DB.

## Running locally

```sh
docker compose up -d
# fastapi     → http://localhost:8000
# temporal-ui → http://localhost:8080
# postgres    → localhost:5432 (user/pass: postgres/postgres, db: codebase_agent)
```

Required env: `OPENAI_API_KEY`. Optional: `TEMPORAL_HOST`, `DATABASE_URL`, `AGENT_SESSION_DB`.

FastAPI reloads on file change (`--reload`); Temporal workflows pick up code changes when the worker restarts (i.e. uvicorn restart).

## Roadmap

Tracked as phased milestones (see conversation history / planning docs):

- **Phase 3** — long-lived chat workflow ✅ *(3.1–3.3 done; 3.4 agent_turn_activity + 3.5 wait_condition loop pending)*
- **Phase 4** — streaming agent turn (`Runner.run_streamed` → AI SDK v6 UI Message Stream parts → per-event persistence)
- **Phase 5** — human-in-the-loop (`ask_user` tool, `pending_actions`, clarification signals)
- **Phase 6** — SSE endpoint (`POST /sessions/:id/messages` returns `text/event-stream`)
- **Phase 7** — cleanup (TTL sweeper, structured logging, rate limiting, `SQLiteSession` → `SQLAlchemySession` for multi-replica)

## Testing

Whenever a new API route is created or an existing one is changed, verify it with `curl` against the test repository:

```
https://github.com/ThomasBenjaminCook/WattAppWebApp
```

Example flow:

```sh
# Create a session
curl -s -X POST http://localhost:8000/sessions \
  -H "Content-Type: application/json" \
  -d '{"repo_url":"https://github.com/ThomasBenjaminCook/WattAppWebApp"}'

# Check status (use the returned session_id)
curl -s http://localhost:8000/sessions/<session_id>

# Fetch messages
curl -s http://localhost:8000/sessions/<session_id>/messages

# Error cases: missing body, unknown id
curl -s -X POST http://localhost:8000/sessions -H "Content-Type: application/json" -d '{}'
curl -s http://localhost:8000/sessions/00000000-0000-0000-0000-000000000000
```

Always test the happy path, expected error responses, and any state transitions (e.g. `indexing` → `ready` → `ended`).

## Deprecated

- `GET /askQuestion` — replaced by the session-based chat flow. Removed along with `CodebaseOnboardingWorkflow`, `WorkflowParams`, `AskParams`, and `ask_agent_activity`.
