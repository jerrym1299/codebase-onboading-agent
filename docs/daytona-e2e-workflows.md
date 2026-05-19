# Daytona E2E Workflows

This runbook explains the current repo-demo indexing, candidate, verification,
and repair flow from the codebase-agent side. It is written for local
development first. You do not need AWS for the local workflows below.

## Current System Shape

The full product flow has two services:

- `hobbesBackend` is the product control plane. It owns org access, GitHub App
  installation records, recipe draft state, Daytona demo sandbox launches, and
  the UI-facing repo-demo endpoints.
- `hobbesCodebaseAgent` owns repo indexing, recipe-candidate generation, and
  recipe-candidate repair after a Daytona verification failure.

The important path today is:

1. User connects/selects a GitHub repo in the Hobbes UI.
2. HobbesBackend creates a repo-demo recipe draft and submits a code-indexing
   job to codebase-agent.
3. Codebase-agent clones/indexes the repo and persists files, chunks,
   summaries, exact line inventory, and repo index metadata.
4. HobbesBackend asks codebase-agent to generate an initial recipe candidate.
5. HobbesBackend converts the candidate into a Daytona launch plan and tries to
   verify it in a real sandbox.
6. If verification fails, HobbesBackend builds a repair bundle and calls
   codebase-agent `POST /recipe-candidates/repair`.
7. Codebase-agent runs the recipe repair loop. With the sandbox agent enabled,
   it creates an isolated execution surface, inspects the repo, runs setup and
   startup commands, and returns `repaired`, `blocked`, or `no_change`.
8. HobbesBackend stores the repaired candidate and can rerun verification.

There are two sandbox concepts:

- Product verification sandbox: created by HobbesBackend for the user-facing
  demo app.
- Repair sandbox: created by codebase-agent only to investigate and repair a
  failed candidate. Locally this can be a Docker sidecar. In dev/prod it uses
  Daytona.

## Local Setup

From `hobbesCodebaseAgent`:

```sh
cp .env.example .env
```

Set at least:

```sh
OPENAI_API_KEY=...
```

For private repos, set GitHub auth:

```sh
GITHUB_TOKEN=...
# or GitHub App credentials:
GITHUB_APP_ID=...
GITHUB_APP_PRIVATE_KEY_BASE64=...
```

Start the local stack:

```sh
docker compose up -d --build
curl -sS http://localhost:8001/health
```

Useful logs:

```sh
docker compose logs -f fastapi
docker compose logs -f index-worker
```

The local FastAPI service runs `main.py` on `http://localhost:8001`. Those
local debug endpoints are not API-key protected.

## Fast Local Checks

Run these before testing live Daytona:

```sh
docker exec hobbescodebaseagent-fastapi-1 \
  python -B -m py_compile \
  services/sandbox_runner.py \
  services/recipe_repair_agent.py \
  services/recipe_repair.py \
  indexing_api.py \
  main.py
```

Deterministic repair contract smoke:

```sh
docker exec hobbescodebaseagent-fastapi-1 \
  python -B scripts/test_recipe_repair.py
```

Fake Daytona runner smoke. This does not call Daytona; it validates our adapter
behavior and repo-specific clone-token wiring:

```sh
docker exec hobbescodebaseagent-fastapi-1 \
  python -B scripts/test_daytona_sandbox_runner.py
```

## Local Repair E2E With Docker Sidecar

Use this when you want to test the agentic repair loop locally without Daytona.
It still runs real shell commands in an isolated Docker container.

Build the sidecar image:

```sh
docker build -t hobbes-verify-sidecar:latest -f dockerfile.sidecar .
```

Set these in `.env`:

```sh
RECIPE_REPAIR_ENABLE_SANDBOX_AGENT=true
RECIPE_REPAIR_SANDBOX_PROVIDER=sidecar
RECIPE_REPAIR_AGENT_MAX_TURNS=24
VERIFY_SANDBOX_IMAGE=hobbes-verify-sidecar:latest
```

Restart FastAPI so the env is loaded:

```sh
docker compose up -d --force-recreate fastapi
```

Run a repair request with an intentionally bad startup command:

```sh
curl -sS --max-time 600 \
  -H "Content-Type: application/json" \
  -X POST http://localhost:8001/recipe-candidates/repair \
  --data-binary @- <<'JSON' | python3 -m json.tool
{
  "metadata": {
    "source": "local_sidecar_repair_smoke"
  },
  "repair_bundle": {
    "schema_version": 1,
    "source": "local_smoke",
    "status": "repair_ready",
    "recipe_id": "local-smoke-recipe",
    "organization_id": "local-smoke-org",
    "candidate_id": "local-smoke-candidate",
    "candidate_version": "v1",
    "repo_context": {
      "provider": "github",
      "repo_url": "https://github.com/iaseth/vite-react-ts-starter.git",
      "repo_full_name": "iaseth/vite-react-ts-starter",
      "branch": "main",
      "root_dir": "",
      "repo_index_id": "local-smoke-index",
      "index_job_id": "local-smoke-job"
    },
    "candidate": {
      "status": "ok",
      "source": "local_smoke",
      "model": "smoke",
      "confidence": 0.2,
      "package_manager": "npm",
      "config": {
        "services": {
          "frontend": {
            "command": "npm run missing",
            "cwd": "",
            "port": 5173,
            "primary": true,
            "preview": true
          }
        }
      },
      "env_template_keys": [],
      "demo": { "path": "/" },
      "warnings": [],
      "evidence": [
        {
          "source": "local_smoke",
          "detail": "Intentional bad command to exercise repair."
        }
      ],
      "evidence_summary": {}
    },
    "execution": {
      "sandbox_run_id": "local-smoke-sandbox-run",
      "run_type": "verification",
      "status": "failed",
      "stage": "startup",
      "preview_urls": [],
      "launch_plan": {
        "services": {
          "frontend": {
            "command": "npm run missing",
            "cwd": "",
            "port": 5173,
            "primary": true,
            "preview": true
          }
        }
      },
      "checks": [{ "name": "frontend_startup", "status": "failed" }],
      "error": "npm ERR! Missing script: missing",
      "metadata": {}
    },
    "repair_history": [],
    "constraints": {
      "max_repair_attempts": 3,
      "attempt_count": 0,
      "allowed_write_scope": "sandbox_workspace",
      "forbidden_actions": [
        "push_to_customer_repo",
        "write_to_production_services",
        "persist_raw_secrets"
      ],
      "secret_policy": "Do not include raw secrets, private keys, tokens, or customer data in repair output."
    },
    "expected_agent_output": {
      "status": "repaired|blocked|no_change",
      "revised_candidate": "Full candidate payload when repaired.",
      "change_summary": "Short explanation.",
      "commands_changed": [],
      "confidence": 0.0,
      "blockers": [],
      "evidence": []
    }
  }
}
JSON
```

Expected result:

- `status` should be `repaired`.
- `revised_candidate.config.services.frontend.command` should become
  `npm run dev -- --host 0.0.0.0 --port 5173 --strictPort`.
- `repair_strategy` should be present.
- `repair_transcript` should show tool calls and sandbox command evidence.

If it returns `blocked`, inspect `blockers`, `evidence`, and `repair_transcript`.

## Live Daytona Checks Without AWS

Use this when you want to verify the Daytona runner itself from your local
machine. This requires only a Daytona API key, not AWS access.

Set in `.env`:

```sh
DAYTONA_API_KEY=...
SANDBOX_RUNNER_PROVIDER=daytona
DAYTONA_SANDBOX_BASE_IMAGE=mcr.microsoft.com/playwright:v1.51.1-noble
DAYTONA_SANDBOX_AUTO_STOP_MINUTES=60
DAYTONA_SANDBOX_AUTO_ARCHIVE_MINUTES=1440
DAYTONA_SANDBOX_AUTO_DELETE_MINUTES=120
```

Restart FastAPI:

```sh
docker compose up -d --force-recreate fastapi
```

Live Daytona runner smoke:

```sh
docker exec hobbescodebaseagent-fastapi-1 \
  python -B scripts/test_daytona_live_runner.py \
  --repo https://github.com/octocat/Hello-World
```

Live Daytona tool smoke:

```sh
docker exec hobbescodebaseagent-fastapi-1 \
  python -B scripts/test_daytona_live_tool.py
```

Both scripts create a real Daytona sandbox, persist a `sandbox_runs` row, run a
command, print the Daytona sandbox id, and then delete the sandbox.

## Live Daytona Repair E2E

To test the actual repair loop with Daytona from local codebase-agent, set:

```sh
RECIPE_REPAIR_ENABLE_SANDBOX_AGENT=true
RECIPE_REPAIR_SANDBOX_PROVIDER=daytona
RECIPE_REPAIR_AGENT_MAX_TURNS=24
DAYTONA_API_KEY=...
```

Restart FastAPI:

```sh
docker compose up -d --force-recreate fastapi
```

Then run the same `POST /recipe-candidates/repair` request from the sidecar
section above. The only difference is that the repair sandbox will be a real
Daytona sandbox instead of a local Docker sidecar.

Expected result for the Vite smoke:

- The agent inspects `package.json`.
- The strategy agent identifies that `npm run missing` is invalid.
- The repair agent runs dependency setup before treating missing local binaries
  like `vite` as blockers.
- The repair agent starts Vite with explicit host/port flags.
- The repair agent verifies `HTTP 200` on `127.0.0.1:5173`.
- The response status is `repaired`.

## Running Through HobbesBackend Locally

For the UI/product flow, run HobbesBackend and point it at local
codebase-agent:

```sh
CODE_INDEXING_API_BASE_URL=http://localhost:8001
CODEBASE_AGENT_API_BASE_URL=http://localhost:8001
CODEBASE_AGENT_REPAIR_TIMEOUT_SECONDS=300
```

`CODE_INDEXING_API_KEY` can be blank for local codebase-agent because `main.py`
does not require auth. In deployed dev, HobbesBackend talks to `indexing_api.py`,
which does require `X-Hobbes-Code-Indexing-Key`.

Local UI flow:

1. Start codebase-agent with Docker Compose.
2. Start HobbesBackend locally.
3. Start the frontend repo-demo UI.
4. Install/select a GitHub repo through the UI.
5. Create a draft and start indexing.
6. Generate a candidate.
7. Verify the candidate.
8. If verification fails, click repair/generate repaired candidate.
9. Re-verify and promote once the candidate works.

When this runs locally, HobbesBackend still owns the product recipe state and
verification sandbox behavior. Codebase-agent only owns indexing, candidate
generation, and repair.

## Inspecting Local State

Recent sandbox runs:

```sh
docker exec hobbescodebaseagent-postgres-1 \
  psql -U postgres -d codebase_agent \
  -c "select id, session_id, provider, external_id, status, created_at from sandbox_runs order by created_at desc limit 10;"
```

Recent sandbox commands:

```sh
docker exec hobbescodebaseagent-postgres-1 \
  psql -U postgres -d codebase_agent \
  -c "select command, cwd, status, exit_code, timed_out, created_at from sandbox_command_runs order by created_at desc limit 20;"
```

Queued/completed index jobs:

```sh
docker exec hobbescodebaseagent-postgres-1 \
  psql -U postgres -d codebase_agent \
  -c "select id, repo_url, status, error_code, created_at, updated_at from repo_index_jobs order by created_at desc limit 10;"
```

Cleanup local sidecar containers:

```sh
docker ps --filter "name=verify-"
docker rm -f <container_name>
```

Do not set `RECIPE_REPAIR_KEEP_SANDBOX=true` unless you intentionally want to
debug the sandbox manually. If you keep Daytona sandboxes alive, delete them in
Daytona after the test.

## Common Failure Modes

- `DAYTONA_API_KEY is present` fails: add the key to `.env` and recreate the
  FastAPI container.
- `SANDBOX_RUNNER_PROVIDER` is not `daytona`: set it in `.env` and recreate
  FastAPI.
- `vite: not found` or similar after startup command: the repair agent should
  run dependency setup first. If it still blocks, inspect `repair_transcript`.
- Clone failure on private repos: set `GITHUB_TOKEN` locally, or use GitHub App
  credentials so codebase-agent can mint repo-scoped tokens.
- The response has no `repair_strategy`: the sandbox agent did not run. Check
  `RECIPE_REPAIR_ENABLE_SANDBOX_AGENT=true` and `OPENAI_API_KEY`.
- Long repair call times out: raise the caller timeout. HobbesBackend uses
  `CODEBASE_AGENT_REPAIR_TIMEOUT_SECONDS`.

## Deployed Dev Notes

The deployed dev path runs codebase-agent behind:

```text
http://hobbes-code-indexing-dev-api-1328544273.us-west-2.elb.amazonaws.com
```

That deployed service uses `indexing_api.py`, reads the API key from
`hobbes-code-indexing-dev/api-key`, and reads Daytona from
`hobbes-code-indexing-dev/daytona-api-key`.

Only people comfortable with AWS should deploy or force-redeploy ECS. Local
development should use the workflows above.
