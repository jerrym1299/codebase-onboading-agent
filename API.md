# Codebase Onboarding Agent — API Documentation

Backend API for long-lived AI chat sessions over **one or more** GitHub repositories. The backend clones, indexes, analyses, extracts wire boundaries from each repo, then runs a deterministic cross-repo matcher and a streaming consolidator that emits a single app-level startup plan. A separate frontend client consumes this API.

---

## Quick Start

```sh
docker compose up -d
# FastAPI     → http://localhost:8000
# Temporal UI → http://localhost:8080
# Postgres    → localhost:5432
```

Required env: `OPENAI_API_KEY`. Optional: `TEMPORAL_HOST`, `DATABASE_URL`, `AGENT_SESSION_DB`.

### Typical Client Flow

```
1. POST /sessions { repo_urls: [...] }       → get session_id
2. Poll GET /sessions/:id                    → wait for status: "ready"
                                              (clone+index+analyze+extract per repo,
                                               then matcher, then consolidator)
3. GET /sessions/:id/startup-plan            → consolidated app-level markdown
4. POST /sessions/:id/messages { content }   → SSE stream of agent events
5. On reconnect: GET /sessions/:id/messages  → full history for hydration
6. Repeat step 4 for each user message

Optional:
- POST /sessions/:id/startup-plan/recompute  → rerun whole pipeline
- POST /sessions/:id/startup-plan/export     → markdown + base64 PDF
- GET  /sessions/:id/repos/:repo_url/...     → per-repo diagnostics
```

---

## Endpoints

### `POST /sessions`

Create a new chat session for one or more repositories. Starts a Temporal workflow that clones, indexes, analyses, and matches every repo in parallel, then runs the streaming consolidator.

**Request**
```json
{ "repo_urls": ["https://github.com/org/repo-a", "https://github.com/org/repo-b"] }
```

Legacy single-repo payload still works (treated as a one-element list):
```json
{ "repo_url": "https://github.com/org/repo" }
```

URLs are normalised (`rstrip("/")`, deduped, sorted). The session is keyed on `repo_set_hash = sha256("\n".join(sorted(repo_urls)))`, so two sessions over the same repo set share the consolidated `app_startup_plans` row.

**Response**
```json
{
  "session_id": "uuid",
  "repo_urls": ["https://github.com/org/repo-a", "https://github.com/org/repo-b"]
}
```

**Errors**
```json
{ "error": "Missing 'repo_urls' (or legacy 'repo_url')." }
```

---

### `GET /sessions/recent`

List recent sessions for the recent sessions page. Results are ordered by `last_seen_at` descending, then `created_at` descending.

**Query params**

| Param | Default | Notes |
|---|---:|---|
| `limit` | `20` | Clamped to `1..100`. |

**Response**
```json
{
  "sessions": [
    {
      "session_id": "uuid",
      "status": "ready",
      "repo_set_hash": "<sha256>",
      "repos": [
        {
          "name": "repo-a",
          "url": "https://github.com/org/repo-a"
        }
      ],
      "repo_names": ["repo-a"],
      "created_at": "2026-04-17T04:03:27.951813+00:00",
      "last_seen_at": "2026-04-17T04:05:12.123456+00:00"
    }
  ]
}
```

---

### `GET /sessions/{session_id}`

Check session status.

**Response**
```json
{ "session_id": "uuid", "status": "indexing" }
```

Status values: `indexing` → `ready` → `ended`.

**Errors**
```json
{ "error": "Session not found." }
```

---

### `GET /sessions/{session_id}/messages`

Fetch full message history for frontend hydration (e.g. on page reload or reconnect).

**Response**
```json
{
  "session_id": "uuid",
  "messages": [
    {
      "id": "uuid",
      "role": "user",
      "parts": [{ "type": "text", "text": "What does this app do?" }],
      "created_at": "2026-04-17T04:03:27.951813+00:00"
    },
    {
      "id": "uuid",
      "role": "assistant",
      "parts": [
        { "type": "tool-input-available", "toolCallId": "...", "toolName": "list_files", "args": "..." },
        { "type": "tool-output-available", "toolCallId": "...", "output": "..." },
        { "type": "text", "text": "This app is a Flask web application that..." }
      ],
      "created_at": "2026-04-17T04:03:27.953560+00:00"
    }
  ]
}
```

---

### `POST /sessions/{session_id}/messages`

Send a user message and receive an SSE stream of agent events in real-time.

**Request**
```json
{ "content": "What tech stack does this app use?" }
```

**Response**: `text/event-stream` (Server-Sent Events)

```
data: {"type": "text-delta", "textDelta": "The"}

data: {"type": "text-delta", "textDelta": " app"}

data: {"type": "tool-input-available", "toolCallId": "call_abc", "toolName": "list_files", "args": "{\"dir_path\":\"/repos/MyApp\"}"}

data: {"type": "tool-output-available", "toolCallId": "call_abc", "output": "['/repos/MyApp/app.py']"}

data: {"type": "data-handoff", "agent": "Explorer"}

data: {"type": "text", "text": "The app uses Flask with a MySQL backend."}

data: {"type": "finish"}
```

**Response Headers**
```
Content-Type: text/event-stream
Cache-Control: no-cache, no-transform
X-Accel-Buffering: no
Connection: keep-alive
```

**Errors**
```json
{ "error": "Missing 'content'." }
{ "error": "Session not found." }
```

**Client Disconnect**: The agent turn keeps running. On reconnect, call `GET /sessions/:id/messages` for full history.

---

### `GET /sessions/{session_id}/startup-plan`

Fetch the consolidated **app-level** startup plan covering every repo in the session. The consolidator emits this as a single markdown document with sections: `# Startup plan: ...`, `## Prerequisites`, `## Env vars`, `## Steps`, `## Dependency graph` (Mermaid), `## Caveats`.

**Response**
```json
{
  "repo_set_hash": "<sha256>",
  "repo_urls": ["https://github.com/org/repo-a", "https://github.com/org/repo-b"],
  "plan_markdown": "# Startup plan: …\n## Prerequisites\n…",
  "graph": {
    "nodes": [
      { "kind": "repo", "id": "https://github.com/org/repo-a", "name": "repo-a" },
      { "kind": "infra", "id": "postgres:DATABASE_URL", "infra_kind": "postgres", "target_env": "DATABASE_URL" }
    ],
    "edges": [
      { "source": "https://github.com/org/repo-a", "target": "postgres:DATABASE_URL",
        "edge_type": "shared_infra", "confidence": 0.9, "evidence": [...], "match_reason": "infra:postgres" }
    ],
    "topo_order": [["postgres:DATABASE_URL"], ["https://github.com/org/repo-a"]],
    "cycle_breaks": []
  },
  "ambiguities": [{ "repo_url": "...", "field": "consumed[3]", "reason": "..." }],
  "orchestration_findings": [{ "repo_url": "...", "file": "...", "parsed_services": [...], "parsed_dependencies": [...] }],
  "analysis_status": "ok",
  "model": "gpt-5.4",
  "error": null,
  "updated_at": "2026-05-12T07:26:14.75761+00:00"
}
```

**Pending**: returns `404 { "status": "pending" }` until the consolidator finishes.

**Errors**
```json
{ "error": "Session not found." }
```

---

### `POST /sessions/{session_id}/startup-plan/recompute`

Force a full re-analysis. Reruns the **whole** pipeline (per-repo clone/index/analyze/extract with `force=True`, then matcher, then consolidator). Returns immediately; watch `GET /sessions/:id` for `status` to flip back to `ready`.

**Request** (optional)
```json
{ "reason": "added a new env var" }
```

**Response** `202 Accepted`
```json
{ "status": "recomputing", "session_id": "uuid" }
```

---

### `POST /sessions/{session_id}/startup-plan/export`

Thin wrapper: read the persisted `app_startup_plans.plan_markdown`, render to PDF via `services/pdf_output.py::write_markdown_pdf`. **No agent runs** — the consolidator already verified the markdown.

**Response**
```json
{
  "session_id": "uuid",
  "repo_urls": ["https://github.com/org/repo-a", "..."],
  "markdown": "# Startup plan: …",
  "pdf_base64": "<base64-encoded PDF bytes>"
}
```

**Pending / errors** same as `GET /sessions/:id/startup-plan`.

---

### `GET /sessions/{session_id}/repos/{repo_url}/startup-plan`

Per-repo diagnostic: returns the persisted `startup_plans` row for one repo in the session. `:repo_url` must be URL-encoded (e.g. `https%3A%2F%2Fgithub.com%2Forg%2Frepo`).

**Response**
```json
{
  "repo_url": "https://github.com/org/repo",
  "plan": { ... },
  "analysis_status": "ok",
  "overall_confidence": null,
  "model": "gpt-5.4",
  "truncations": [],
  "error": null,
  "updated_at": "..."
}
```

**Errors**
```json
{ "error": "Repo not part of session.", "repo_urls": [...] }
{ "status": "pending" }
```

---

### `GET /sessions/{session_id}/repos/{repo_url}/boundaries`

Per-repo diagnostic: returns the `repo_boundaries` row produced by `boundary_extractor_agent`. Same URL-encoding rule for `:repo_url`.

**Response**
```json
{
  "repo_url": "https://github.com/org/repo",
  "report": {
    "repo_url": "https://github.com/org/repo",
    "exposed": [{ "kind": "http", "method": "GET", "path": "/api/users", "handler": "users.py:42" }],
    "consumed": [
      { "kind": "http", "target_env": "BACKEND_URL", "resolved": "http://localhost:8000", "resolved_from": ".env.example", "path": "/api" },
      { "kind": "db", "engine": "postgres", "target_env": "DATABASE_URL", "resolved": null, "resolved_from": null }
    ],
    "dev_proxy": [],
    "required_services": [{ "kind": "postgres", "via": "DATABASE_URL" }],
    "ambiguities": []
  },
  "analysis_status": "ok",
  "model": "gpt-5.4",
  "error": null,
  "updated_at": "..."
}
```

---

## SSE Event Types

Events follow the AI SDK v6 UI Message Stream protocol.

| Event Type | Fields | When |
|---|---|---|
| `text-delta` | `textDelta: string` | Each token of agent text output (chat turns and consolidator) |
| `tool-input-available` | `toolCallId`, `toolName`, `args` | Agent calls a tool |
| `tool-output-available` | `toolCallId`, `output` | Tool returns a result |
| `data-handoff` | `agent: string` | Agent hands off to a sub-agent |
| `data-needs-input` | `pendingId`, `question`, `options` | Agent asks user a clarifying question (see Human-in-the-Loop) |
| `text` | `text: string` | Final complete text of a message segment |
| `data-repo-progress` | `repo_url`, `stage` | Per-repo lifecycle: `cloning|cloned|indexing|indexed|extracting_boundaries|boundaries_extracted` |
| `data-startup-plan-updated` | `updatedAt` | Per-repo `startup_plans` row written or refreshed |
| `data-graph-built` | `node_count`, `edge_count`, `ambiguity_count` | Cross-repo matcher finished; placeholder `app_startup_plans` row exists |
| `data-consolidator-started` | `repo_set_hash` | Consolidator agent is about to stream — text-deltas + tool events follow until `data-app-plan-updated` |
| `data-app-plan-updated` | `updatedAt`, `repo_set_hash` | Consolidated app-level markdown persisted |
| `finish` | — | End of stream; close the SSE connection |

> **Note:** the indexing/analysis events (`data-repo-progress`, `data-startup-plan-updated`, `data-graph-built`, `data-consolidator-*`, `data-app-plan-updated`) only flow to a client that's subscribed to the per-session pubsub when they fire. To watch indexing progress live without sending a chat message, use `GET /sessions/{session_id}/events` after `POST /sessions`; it terminates when the session reaches `ready`, `failed`, or `ended`.

---

## Sessions & Lifecycle

Each session maps 1:1 to a Temporal workflow (`CodebaseChatWorkflow`) identified by `chat-{session_id}`.

### Status Transitions

```
POST /sessions
    → status: "indexing"  (cloning + chunking + embedding)
    → status: "ready"     (waiting for messages)
    → status: "ended"     (end_session signal received)
```

### Ending a Session

Send the `end_session` signal via Temporal:

```sh
docker compose exec temporal tctl --address temporal:7233 \
  workflow signal --workflow_id "chat-<session_id>" --name end_session
```

Or programmatically via the Temporal client. (A future `DELETE /sessions/:id` endpoint can wrap this.)

---

## Human-in-the-Loop

The agent can ask the user clarifying questions mid-conversation. No special endpoint is needed — the user just sends another message.

### How It Works

1. **Agent decides to ask**: If a question is ambiguous, the agent calls the `ask_user(question, options)` tool.

2. **Frontend receives event**: A `data-needs-input` SSE event arrives:
   ```json
   {
     "type": "data-needs-input",
     "pendingId": "uuid",
     "question": "Which flow would you like me to trace?",
     "options": ["User login flow", "Data upload flow", "Other"]
   }
   ```

3. **Frontend shows UI**: Render the question and options (buttons, dropdown, or free text).

4. **User responds**: `POST /sessions/:id/messages { "content": "User login flow" }` — a normal message.

5. **Agent continues**: The workflow auto-resolves the pending action and starts a new agent turn with the user's answer. The agent has full session memory and knows what it asked.

### Querying Pending Actions

The `get_pending` Temporal query returns open actions:

```sh
docker compose exec temporal tctl --address temporal:7233 \
  workflow query --workflow_id "chat-<session_id>" --query_type get_pending
```

Returns:
```json
[{
  "id": "uuid",
  "question": "Which flow would you like me to trace?",
  "options": ["User login flow", "Data upload flow", "Other"]
}]
```

### Cleanup

- When the user replies, open pending actions are auto-resolved (`status: 'resolved'`).
- On session end, any remaining open actions are cancelled (`status: 'cancelled'`).

---

## Integrating with AI SDK (Vercel)

The SSE stream follows the AI SDK v6 UI Message Stream protocol, so a Next.js frontend can consume it with minimal glue.

### Using `useChat` with a Custom API

```tsx
import { useChat } from '@ai-sdk/react';

export default function Chat({ sessionId }: { sessionId: string }) {
  const { messages, input, setInput, sendMessage, isLoading } = useChat({
    api: `${API_BASE}/sessions/${sessionId}/messages`,
    // The backend streams AI SDK v6 compatible events
  });

  return (
    <div>
      {messages.map((m) => (
        <div key={m.id}>
          <strong>{m.role}:</strong> {m.content}
        </div>
      ))}
      <form onSubmit={(e) => { e.preventDefault(); sendMessage({ text: input }); setInput(''); }}>
        <input value={input} onChange={(e) => setInput(e.target.value)} />
        <button type="submit" disabled={isLoading}>Send</button>
      </form>
    </div>
  );
}
```

> **Note**: The SSE events use AI SDK v6 part type names (`text-delta`, `tool-input-available`, `tool-output-available`). If your AI SDK version expects different field names, you may need a thin mapping layer in the frontend or a proxy endpoint.

### Hydration on Reconnect

```tsx
// On mount or reconnect, fetch full history
const res = await fetch(`${API_BASE}/sessions/${sessionId}/messages`);
const { messages } = await res.json();
// Populate the chat UI with messages[].parts
```

### Handling `data-needs-input`

```tsx
// In your SSE event handler, watch for data-needs-input
if (event.type === 'data-needs-input') {
  showClarificationUI({
    pendingId: event.pendingId,
    question: event.question,
    options: event.options, // render as buttons or dropdown
  });
}

// When user picks an option, just send it as a normal message
sendMessage({ text: "User login flow" });
```

---

## Database Tables

| Table | Purpose |
|---|---|
| `sessions` | One row per chat session. Fields: `id`, `repo_url`, `status`, `created_at`, `last_seen_at`. |
| `messages` | Chat messages with AI-SDK-style `parts[]` JSONB. Fields: `id`, `session_id`, `role`, `parts`, `created_at`. |
| `pending_actions` | Tracks agent questions awaiting user response. Fields: `id`, `session_id`, `kind`, `payload`, `status`, `resolved_value`, `created_at`, `resolved_at`. |
| `tenants` | Customer/workspace owner for indexed source and learned facts. |
| `repo_connections` | Tenant-scoped repo connection metadata keyed by repo URL/provider. |
| `repo_indexes` | Versioned index records keyed by repo connection and manifest hash. |
| `repo_latest_indexes` | Latest serving pointer for each repo connection. |
| `repo_index_jobs` | Durable queue/job record with status, attempts, metrics, and errors. |
| `code_chunks` | AST-level code chunks + 3072-dim embeddings (pgvector), keyed by content-addressed chunk hashes. |
| `dir_summaries` | Per-directory LLM-generated summaries + embeddings. |
| `repo_index_runs` | Append-only content manifest history for each indexing/debug run. |
| `repo_files` | Latest content-addressed file inventory keyed by `(repo_url, file_path)`. |
| `repo_text_lines` | Latest non-empty line inventory for exact string and regex lookup. |
| `repo_chunk_manifests` | Latest content-addressed chunk inventory keyed by `(repo_url, chunk_sha256)`. |
| `repo_embedding_cache` | Repo-scoped embedding cache keyed by `(repo_url, embedding_sha256)`. |

---

## Agent Architecture

```
Router (gpt-4.1-mini)
  ├── Explorer  — find files, symbols, paths
  ├── Explainer — summarize, explain how things work
  └── Tracer    — follow execution paths, call chains
```

All agents have access to `ask_user` for clarification. The Router decides which sub-agent to delegate to based on the user's question. Agents can hand off between each other within a single turn.

### Agent Tools

| Tool | Available To | Purpose |
|---|---|---|
| `list_files(dir_path, glob)` | Explorer, Explainer | Find files by pattern |
| `read_file(path, start, end)` | Explorer, Explainer, Tracer | Read file contents |
| `search_code(dir_path, query)` | Explorer | Regex search across files |
| `search_exact_indexed(query, repo_url, limit, regex, path, language)` | Explorer, Tracer | Exact string or regex search over persisted line inventory |
| `find_references(symbol, dir_path)` | Tracer | Find all references to a symbol |
| `get_dependencies(file_path)` | Tracer | Extract import dependencies |
| `search_indexed(query, repo_url, k)` | Explainer | Semantic search over pgvector chunks |
| `search_dir_summaries(query, repo_url, k)` | Explainer | Search directory-level summaries |
| `git_log(path, limit)` | Explainer | Recent git commit history |
| `ask_user(question, options)` | All agents | Ask the user a clarifying question |

---

## Legacy / Debug Endpoints

These endpoints predate the session-based flow and are useful for debugging:

| Endpoint | Purpose |
|---|---|
| `GET /walkrepo?repo_url=...` | Directory tree of a repo |
| `POST /repo-connections` | Create or update a tenant-scoped repo connection |
| `POST /repo-index-jobs` | Create a durable repo indexing job |
| `GET /repo-index-jobs/{job_id}` | Read indexing job status, metrics, and errors |
| `GET /chunks?repo_url=...` | Chunk + embed a repo, return metadata |
| `GET /manifest?repo_url=...` | Chunk without embeddings, return/persist file and chunk hashes |
| `GET /ast?repo_url=...` | Tree-sitter AST dump |
| `GET /explore?repo_url=...&query=...` | One-shot agent query (no session) |
| `GET /search?repo_url=...&query=...` | Raw pgvector similarity search |
| `GET /search-exact?repo_url=...&query=...` | Exact string or regex search over indexed lines |
