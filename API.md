# Codebase Onboarding Agent — API Documentation

Backend API for long-lived AI chat sessions over any GitHub repository. A separate frontend client consumes this API.

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
1. POST /sessions { repo_url }           → get session_id
2. Poll GET /sessions/:id                → wait for status: "ready"
3. POST /sessions/:id/messages { content }→ SSE stream of agent events
4. On reconnect: GET /sessions/:id/messages → full history for hydration
5. Repeat step 3 for each user message
```

---

## Endpoints

### `POST /sessions`

Create a new chat session for a repository. Starts a Temporal workflow that clones and indexes the repo.

**Request**
```json
{ "repo_url": "https://github.com/org/repo" }
```

**Response**
```json
{ "session_id": "uuid" }
```

**Errors**
```json
{ "error": "Missing 'repo_url'." }
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

## SSE Event Types

Events follow the AI SDK v6 UI Message Stream protocol.

| Event Type | Fields | When |
|---|---|---|
| `text-delta` | `textDelta: string` | Each token of agent text output |
| `tool-input-available` | `toolCallId`, `toolName`, `args` | Agent calls a tool |
| `tool-output-available` | `toolCallId`, `output` | Tool returns a result |
| `data-handoff` | `agent: string` | Agent hands off to a sub-agent |
| `data-needs-input` | `pendingId`, `question`, `options` | Agent asks user a clarifying question (see Human-in-the-Loop) |
| `text` | `text: string` | Final complete text of a message segment |
| `finish` | — | End of stream; close the SSE connection |

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
