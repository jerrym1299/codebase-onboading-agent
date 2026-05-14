# Codebase Onboarding Agent

Backend API for long-lived AI chat sessions over GitHub repositories. Given a
repo URL, the app clones the repository, chunks source files, embeds the chunks
into Postgres/pgvector, generates directory summaries, and serves a streaming
chat endpoint backed by a router of specialist agents.

This repository is the backend only. There is no npm or frontend build step in
this project.

## What It Does

- Creates one chat session per GitHub repository.
- Uses Temporal to run durable clone, indexing, and chat-turn workflow steps.
- Stores source chunks, file inventories, exact line search, and directory
  summaries in Postgres with pgvector.
- Streams agent responses over Server-Sent Events using AI SDK style message
  parts.
- Supports human-in-the-loop clarification through pending actions.

## Agents Overview

- `Router`: Entry point for user messages. Chooses whether to hand off to an
  explorer, explainer, or tracer agent, and asks clarifying questions when the
  request is ambiguous.
- `Explorer`: Finds exact files, symbols, and code locations with file listing,
  regex search, semantic search, and targeted reads.
- `Explainer`: Synthesizes how the codebase works using indexed code chunks,
  directory summaries, file reads, and git history.
- `Tracer`: Follows execution paths from a symbol or file location using
  references, dependencies, and source reads.

## Requirements

- Docker and Docker Compose for the default local setup.
- Python 3.12 if running the API outside Docker.
- An `OPENAI_API_KEY`.
- Optional `GITHUB_TOKEN` for private repositories or higher GitHub rate limits.

## Install

For Docker-based development, copy the example environment file and fill in your
OpenAI key:

```sh
cp .env.example .env
```

For local Python development, create a virtual environment and install the
backend dependencies:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run With Docker Compose

```sh
docker compose up -d
```

Services:

- FastAPI backend: `http://localhost:8001`
- Temporal UI: `http://localhost:8080`
- Postgres: `localhost:5432` (`postgres` / `postgres` / `codebase_agent`)

The `fastapi` container runs `uvicorn main:app --reload`, so code changes in the
workspace are picked up automatically.

## Run Locally

If you want to run FastAPI on your host machine, keep Postgres and Temporal
running in Docker and point the app at them:

```sh
docker compose up -d postgres temporal temporal-ui

export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/codebase_agent
export TEMPORAL_HOST=localhost:7233
export OPENAI_API_KEY=your_key_here

uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## Basic API Flow

Create a session:

```sh
curl -s -X POST http://localhost:8001/sessions \
  -H "Content-Type: application/json" \
  -d '{"repo_url":"https://github.com/ThomasBenjaminCook/WattAppWebApp"}'
```

Poll until the session is ready:

```sh
curl -s http://localhost:8001/sessions/<session_id>
```

Send a message and consume the SSE stream:

```sh
curl -N -X POST http://localhost:8001/sessions/<session_id>/messages \
  -H "Content-Type: application/json" \
  -d '{"content":"where is the entry point?"}'
```

Hydrate the stored transcript:

```sh
curl -s http://localhost:8001/sessions/<session_id>/messages
```

## Development Notes

- Repo clones are stored under `/repos/<repo_name>` inside the running
  environment.
- The OpenAI Agents SDK session store defaults to `agent_sessions.db`.
- Indexing stores a content-addressed manifest, exact line-search inventory,
  and cached embeddings for unchanged chunks.
- Debug endpoints include `/walkrepo`, `/chunks`, `/manifest`, `/ast`,
  `/explore`, `/search`, and `/search-exact`.
- Run `python3 scripts/eval_indexing.py` against the Docker stack to validate
  manifest stability, DB persistence, exact search, and embedding-cache
  behavior.
- Add `--with-openai` to that eval when the FastAPI container has an
  `OPENAI_API_KEY`; it validates real embeddings and vector search.

## Future Work

- Detect when a GitHub repository has changed and reclone or re-index it.
