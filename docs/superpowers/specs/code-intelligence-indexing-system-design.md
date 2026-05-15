# Customer-Aware Code Intelligence Indexing System

## Purpose

This system is the code intelligence layer for Hobbes customer onboarding and
interactive sandbox generation.

The goal is not generic repository search. The goal is to build a
customer-aware, change-aware, verification-aware index that helps agents answer:

- How does this customer application run?
- Where are the product flows, screens, APIs, services, env vars, and data
  boundaries?
- Which actions must be stubbed, disabled, or made read-only in a demo sandbox?
- What changed since the last successful analysis?
- Which previously verified assumptions are still valid?
- What should a human verify before we trust the generated sandbox plan?

The index should become smarter over time as customers change code and humans
verify facts. It should not be a disposable snapshot.

## Research Synthesis

The major systems and papers point to the same shape:

1. **Incremental, content-addressed indexing is foundational.**
   Cursor describes using Merkle trees, similarity hashes, and content proofs to
   reuse large codebase indexes while ensuring users only see content they can
   prove they have access to. Our current file/chunk SHA design is directionally
   right, but we should evolve it into directory-level Merkle roots and explicit
   tenant/repo/version access boundaries.

2. **Exact and regex search remain critical for agents.**
   Cursor's fast regex work frames the issue well: `ripgrep` is excellent, but
   scanning every file in a large repo can stall an interactive agent. Sourcegraph
   uses Zoekt trigram indexes for fast code search. GitHub's newer code search
   supports substring, regex, and symbol search. Our `repo_text_lines` table plus
   `pg_trgm` is a good first production database implementation; at very large
   scale we may graduate to a dedicated trigram/postings engine.

3. **Semantic search helps agents, but it is not enough.**
   Cursor reports semantic search improving coding-agent accuracy and user
   outcomes, but it is positioned alongside grep/regex, not as a replacement.
   We need hybrid retrieval: exact text, symbols, graph edges, summaries, and
   embeddings.

4. **Static code intelligence needs structural data, not just chunks.**
   GitHub's code navigation uses tree-sitter-based definitions and references.
   Sourcegraph distinguishes search-based navigation from precise code
   navigation via language-specific indexes like SCIP. We should start with
   tree-sitter-derived symbols/imports/routes and leave room for precise
   language indexers later.

5. **Agents need dynamic context discovery.**
   Cursor's context work emphasizes small, targeted tool responses and letting
   the agent pull what it needs. Our tools should return ranked handles,
   line ranges, paths, and provenance rather than dumping large JSON blobs or
   full summaries into context.

6. **Learning requires evals and production feedback loops.**
   Cursor's harness work uses offline eval suites, online experiments, tool
   error metrics, cache-hit rates, and user-retention signals. For Hobbes, the
   analogous signals are retrieval quality, setup-plan correctness, human
   verification outcomes, sandbox safety regressions, and whether later code
   changes invalidate prior facts.

7. **For high-throughput customer indexing, queue-based infrastructure is the
   right first production primitive.**
   AWS SQS gives durable work buffering, visibility timeouts, retries, and DLQs.
   ECS workers can scale on backlog. Temporal or Step Functions can still be
   useful for later agent/HITL workflows, but the indexing path should be an
   idempotent queue worker system.

## Current State

Already implemented locally:

- Content-addressed file/chunk manifests.
- `repo_index_runs`, `repo_files`, `repo_chunk_manifests`.
- Repo-scoped embedding cache keyed by `embedding_sha256`.
- Config/docs indexing for TOML, env examples, config files, CSS/HTML/docs.
- Exact line inventory in `repo_text_lines`.
- `/manifest`, `/chunks`, `/search`, `/search-exact`.
- OpenAI embedding smoke evals and larger repo evals on `next-learn` and
  `flask`.

Important gaps:

- No tenant model.
- No repo connection model.
- No durable index job table.
- No SQS/worker production path.
- No static symbol/import/route/env inventory.
- No longitudinal diff model.
- No human verification memory.
- No quality scoring beyond manifest/cache/exact-search smoke checks.

## Design Principles

1. **Every row is tenant-aware.**
   Customer source code and learned facts must be scoped by tenant/customer and
   repo connection. We should not depend on `repo_url` alone.

2. **Indexes are versioned states, not mutable snapshots.**
   Latest tables are useful for serving, but every important fact must be
   traceable to a commit SHA and manifest SHA.

3. **Incremental by default.**
   Re-indexing should compute a manifest delta first, reuse unchanged file,
   chunk, symbol, embedding, and summary data, and only recompute misses.

4. **Structured facts need provenance.**
   Any setup command, env var, route, sandbox safety rule, or verification
   finding must store source paths/lines, commit/manifest, confidence, and who or
   what verified it.

5. **Hybrid retrieval beats any single index.**
   Use exact text, symbols, graph edges, runtime facts, summaries, and embeddings
   together.

6. **Tool outputs should be context-light.**
   The agent should receive small, ranked results with handles and line ranges,
   then explicitly read deeper context when needed.

7. **Learning is a first-class data product.**
   Human corrections and verified facts are durable product data, not ephemeral
   chat memory.

8. **Quality gates ship with features.**
   Every new index layer must add evals that prove the agent can answer known
   questions better or with fewer tokens/tool calls.

## Target Architecture

```text
GitHub App / manual repo URL
        |
        v
Index API ---------------> Postgres / pgvector
  |                         - jobs
  |                         - manifests
  |                         - files/chunks/lines
  |                         - symbols/imports/routes/env
  |                         - verified facts
  v
SQS index queue + DLQ
        |
        v
ECS indexing workers
  - clone repo
  - compute manifest/Merkle tree
  - build deltas
  - build exact/static/semantic indexes
  - persist versioned artifacts
  - emit metrics/events
        |
        v
Agent analysis / HITL layer
  - reads index
  - proposes setup/sandbox plan
  - creates verification tasks
  - invalidates prior facts when code changes
```

The indexing worker path should not require Temporal. Temporal or Step Functions
can be introduced for the agent verification workflow later if the HITL process
becomes multi-step, cancellable, and long-running.

## Core Data Model

The exact column names can evolve, but these entities should exist before we
scale to many customers.

### Tenancy and Repo Connections

`tenants`

- `id`
- `name`
- `plan`
- `created_at`

`repo_connections`

- `id`
- `tenant_id`
- `provider` (`github`, later `gitlab`, `bitbucket`, `custom_git`)
- `repo_url`
- `provider_repo_id`
- `default_branch`
- `installation_id`
- `access_status`
- `created_at`
- `updated_at`

### Index Jobs

`repo_index_jobs`

- `id`
- `tenant_id`
- `repo_connection_id`
- `requested_by`
- `trigger` (`manual`, `webhook`, `scheduled`, `deploy_smoke`)
- `target_ref`
- `target_commit_sha`
- `status` (`queued`, `cloning`, `manifesting`, `indexing`, `embedding`,
  `summarizing`, `complete`, `failed`, `cancelled`)
- `attempt_count`
- `priority`
- `started_at`
- `completed_at`
- `error_code`
- `error_message`
- `metrics jsonb`

Use a DB uniqueness constraint or lock to dedupe
`tenant_id + repo_connection_id + target_commit_sha`.

### Versioned Indexes

`repo_indexes`

- `id`
- `tenant_id`
- `repo_connection_id`
- `commit_sha`
- `branch`
- `manifest_sha256`
- `root_merkle_sha256`
- `file_count`
- `chunk_count`
- `line_count`
- `symbol_count`
- `embedding_model`
- `status`
- `created_at`

`repo_index_deltas`

- `id`
- `tenant_id`
- `repo_connection_id`
- `from_index_id`
- `to_index_id`
- `files_added`
- `files_removed`
- `files_changed`
- `symbols_added`
- `symbols_removed`
- `symbols_changed`
- `routes_changed`
- `env_vars_changed`
- `risk_flags jsonb`
- `created_at`

### Content Index

Existing tables should gain `tenant_id`, `repo_connection_id`, and
`repo_index_id` where appropriate:

- `repo_files`
- `repo_text_lines`
- `repo_chunk_manifests`
- `code_chunks`
- `repo_embedding_cache`

The serving path can keep latest rows keyed by repo connection, but versioned
rows are needed for deltas, invalidation, and auditability.

### Static Structure Index

`repo_symbols`

- `tenant_id`
- `repo_connection_id`
- `repo_index_id`
- `file_path`
- `symbol_name`
- `symbol_kind` (`function`, `class`, `method`, `component`, `type`,
  `interface`, `constant`, `module`)
- `language`
- `parent_symbol`
- `exported boolean`
- `start_line`
- `end_line`
- `signature`
- `docstring`
- `symbol_sha256`

`repo_imports`

- `tenant_id`
- `repo_connection_id`
- `repo_index_id`
- `source_file_path`
- `imported_module`
- `resolved_file_path`
- `import_kind` (`package`, `relative`, `absolute`, `dynamic`)
- `language`
- `line_number`

`repo_routes`

- `tenant_id`
- `repo_connection_id`
- `repo_index_id`
- `framework`
- `method`
- `path`
- `handler_symbol`
- `file_path`
- `start_line`
- `auth_required`
- `side_effect_kind` (`read`, `write`, `external_call`, `unknown`)
- `risk_flags`

`repo_env_vars`

- `tenant_id`
- `repo_connection_id`
- `repo_index_id`
- `name`
- `source_file_path`
- `line_number`
- `required`
- `default_value`
- `example_value`
- `consumer_symbol`
- `confidence`

`repo_runtime_facts`

- `tenant_id`
- `repo_connection_id`
- `repo_index_id`
- `fact_kind` (`install_command`, `start_command`, `test_command`,
  `migration_command`, `seed_command`, `service`, `port`, `health_check`)
- `value jsonb`
- `source_refs jsonb`
- `confidence`

### Customer Knowledge and Verification Memory

`repo_verified_facts`

- `id`
- `tenant_id`
- `repo_connection_id`
- `fact_kind`
- `statement`
- `structured_value jsonb`
- `source_refs jsonb`
- `verified_by` (`agent`, `human`, `test`, `customer`)
- `verified_by_user_id`
- `confidence`
- `applies_from_index_id`
- `last_valid_index_id`
- `status` (`active`, `needs_reverification`, `superseded`, `rejected`)
- `invalidation_rule jsonb`
- `created_at`
- `updated_at`

`verification_tasks`

- `id`
- `tenant_id`
- `repo_connection_id`
- `repo_index_id`
- `task_kind` (`setup`, `env_var`, `sandbox_safety`, `api_stub`,
  `seed_data`, `flow_mapping`, `route_risk`)
- `question`
- `context_refs jsonb`
- `suggested_answer jsonb`
- `status` (`open`, `answered`, `verified`, `rejected`, `obsolete`)
- `priority`
- `created_by_run_id`
- `created_at`
- `resolved_at`

`agent_analysis_runs`

- `id`
- `tenant_id`
- `repo_connection_id`
- `repo_index_id`
- `run_kind` (`startup_plan`, `sandbox_plan`, `change_impact`,
  `verification_generation`)
- `model`
- `prompt_version`
- `status`
- `input_refs jsonb`
- `output jsonb`
- `metrics jsonb`
- `created_at`

## Index Layers

### Layer 1: Content and Manifest

What we have now should become tenant/version aware:

- filtered file inventory
- file hashes
- chunk hashes
- embedding hashes
- root Merkle hash
- directory-level Merkle nodes
- latest index pointer

Directory-level Merkle nodes let us quickly identify which subtrees changed and
eventually support safe index reuse inside an organization.

### Layer 2: Exact Text Search

Current implementation:

- `repo_text_lines`
- non-empty line storage
- substring/regex endpoint
- optional `pg_trgm` index

Next improvements:

- store `repo_index_id`
- add file/path/language filters everywhere
- return compact snippets with stable result IDs
- add top-N ranking for exact matches, not only path order
- evaluate whether Postgres `pg_trgm` remains enough at expected scale

### Layer 3: Static Symbols and Imports

Use tree-sitter first because we already have parsers and GitHub validates the
approach for broad code navigation. Add language-specific extractors where
needed:

- Python: functions, classes, methods, decorators, imports, Flask/FastAPI routes.
- JS/TS/TSX: functions, classes, React components, exports, imports, Next.js
  routes.
- Ruby/Rails, PHP/Laravel, Go, Java later based on customer demand.

The first goal is not perfect compiler precision. It is high recall, stable
paths/lines, and enough structure for the agent to navigate intelligently.

### Layer 4: Runtime and Setup Facts

Extract facts from:

- package manifests (`package.json`, `pyproject.toml`, `requirements.txt`,
  `Gemfile`, `go.mod`, lockfiles)
- `Dockerfile`, `docker-compose.yml`, `Makefile`
- `.env.example`, `.env.sample`, README docs
- framework conventions
- test files and CI files

Facts should be structured and source-linked. The agent should be able to say:

> I believe `DATABASE_URL` is required because it is read in `settings.py:12`
> and listed in `.env.example:3`.

### Layer 5: Semantic Retrieval

Keep embeddings, but organize semantic retrieval into multiple collections:

- code chunks
- symbol summaries
- directory summaries
- route/API summaries
- runtime/setup summaries
- verified customer facts

Use content-addressed embedding cache for all text types, not only code chunks.
Track `embedding_model`, dimensions, token count, and cost attribution.

### Layer 6: Knowledge Graph

Build graph edges over indexed facts:

- file defines symbol
- symbol imports module/file
- route handled by symbol
- symbol reads env var
- command starts service
- service exposes port
- route writes external system
- verification fact applies to route/symbol/file

This enables impact analysis:

> This commit changed `CampaignLaunchService`; that service is reachable from
> `POST /campaigns/:id/launch`; prior sandbox fact "launch is disabled" must be
> rechecked.

### Layer 7: Change Intelligence

Every reindex should produce a human-readable and machine-readable delta:

- content changes
- dependency changes
- route/API changes
- setup/runtime changes
- safety-sensitive changes
- facts invalidated
- recommended verification tasks

Risk flags should include:

- auth/session changes
- billing/payment changes
- email/SMS sending changes
- CRM/writeback changes
- campaign launch or workflow trigger changes
- export/download changes
- webhook/integration changes
- migrations/schema/seed changes

### Layer 8: Learning and Adaptation

Learning data should update retrieval and reasoning:

- verified setup commands boost confidence for future runs
- rejected agent assumptions reduce future confidence
- files used in successful answers get higher retrieval priors
- frequently invalidated facts get stricter verification rules
- customer-specific safety policies become persistent facts
- changed code paths re-open targeted verification tasks

This is not model fine-tuning first. It is a durable, queryable memory layer with
provenance and invalidation.

## Query and Tool Surface

The agent should get a small set of strong tools:

- `search_exact_indexed(query, filters)`: exact/regex snippets.
- `search_semantic_indexed(query, filters)`: semantic chunk/symbol/summary
  retrieval.
- `search_symbols(name_or_query, filters)`: functions, classes, components,
  handlers.
- `get_symbol(symbol_id)`: definition, signature, source range, imports,
  callers/references when available.
- `get_file_context(file_path)`: file manifest, symbols, imports, routes,
  env vars, summaries.
- `get_route_context(route_id)`: handler, side effects, env vars, downstream
  calls, safety flags.
- `get_runtime_facts(repo_connection_id)`: install/start/test/migrate/seed
  facts.
- `get_verified_facts(scope)`: customer memory relevant to a file, route,
  flow, or setup task.
- `get_change_impact(from_index, to_index)`: invalidated facts and risk flags.
- `create_verification_task(payload)`: ask a targeted human verification
  question.

Tool response rules:

- return IDs, paths, line ranges, confidence, and short snippets
- avoid full-file dumps unless explicitly requested
- always include provenance
- support follow-up reads by handle

## Indexing Job Flow

1. API receives repo connection, webhook, scheduled refresh, or manual reindex.
2. API resolves target commit SHA.
3. API upserts `repo_index_jobs`.
4. API dedupes existing queued/running jobs for same tenant/repo/commit.
5. API sends SQS message with `job_id`.
6. Worker receives message and obtains DB lock on `tenant_id + repo_connection`.
7. Worker updates job status and extends SQS visibility while running.
8. Worker clones repo into ephemeral workspace.
9. Worker computes manifest and Merkle tree.
10. Worker compares previous index and builds delta.
11. Worker reuses unchanged artifacts.
12. Worker recomputes exact/static/semantic indexes for changed files.
13. Worker writes versioned index rows and latest-serving rows.
14. Worker runs change-impact rules and invalidates affected facts.
15. Worker emits metrics and deletes SQS message.

Retries must be idempotent. SQS is at-least-once delivery, so DB locks and
idempotent writes are mandatory.

## Scaling and Fairness

Initial production path:

- SQS standard queue + DLQ.
- ECS worker autoscaling on queue age/backlog.
- Postgres advisory lock or explicit `repo_locks` row per tenant/repo.
- Per-tenant concurrency limits in DB.
- Global OpenAI embedding concurrency limit.
- GitHub clone/API backoff.
- Job priority for paid plans and customer demos.

Fairness controls:

- never allow one tenant's repo flood to starve all workers
- cap simultaneous jobs per tenant
- cap simultaneous jobs per repo connection
- expose queue age and active job count per tenant

For repos whose indexing exceeds SQS's practical processing window, split the
work into sub-jobs:

- manifest job
- static index job
- embedding job batches
- summary job
- analysis job

## Security and Privacy

Required guarantees:

- Tenant isolation at every query and table boundary.
- Customer source indexed only for authorized repo connections.
- GitHub App installation tokens are short-lived and scoped.
- Source chunks/lines are treated as sensitive customer data.
- Delete request removes source-derived rows, embeddings, and verified facts
  unless contractually retained.
- No cross-tenant index reuse initially.
- If we later reuse indexes across related workspaces, implement content proofs
  before serving reused results.
- Secrets are never persisted as raw values when detected from code; store names,
  source references, required/default metadata, and redacted examples.

Open question:

- Whether to store full source lines/chunks long term. The current design does.
  For enterprise customers, we may need a mode that stores structural metadata
  and embeddings but can re-fetch source from GitHub on demand.

## Quality and Evaluation Plan

The eval suite should become a product gate, not a smoke test.

### Offline Golden Repo Evals

Use repos that cover framework patterns:

- `octocat/Spoon-Knife`: tiny deterministic smoke.
- `vercel/next-learn`: Next.js/React/package routing.
- `pallets/flask`: Python package, docs, config, route examples.
- Add Rails, Django, FastAPI, Express, and monorepo fixtures.
- Add private/internal customer-like fixture repos with known sandbox flows.

### Retrieval Evals

Metrics:

- precision@k
- recall@k
- MRR
- expected file/path found
- expected line range overlap
- token cost
- tool-call count
- latency

Task categories:

- symbol lookup
- route lookup
- env var detection
- setup command extraction
- dependency tracing
- exact error string lookup
- natural-language feature lookup
- unsafe action detection
- changed-code impact analysis

### Agent Workflow Evals

Evaluate complete outputs:

- startup plan correctness
- sandbox safety plan correctness
- generated verification task quality
- change-impact summary correctness
- whether agent cites correct files/lines

Use trace grading and repeatable datasets once agent workflows stabilize.

### Production Feedback

Track:

- human accepts/rejects verification suggestions
- human edits to setup/sandbox plans
- failed sandbox launches
- stale facts detected after code changes
- retrieval clicks/usefulness
- cache hit rates
- OpenAI token and embedding spend per tenant/repo
- queue age and time to first useful answer

## Phased Implementation Plan

### Phase 0: Current Foundation

Done:

- content manifests
- embedding cache
- exact lines
- config/docs indexing
- basic evals

### Phase 1: Tenant and Job Model

Add:

- `tenant_id`, `repo_connection_id`, `repo_index_id` model
- `repo_index_jobs`
- durable job status endpoint
- idempotent latest index pointer
- local worker entrypoint that can process one job by ID

This prepares both product and infra work without changing retrieval quality.

### Phase 2: Static Inventory

Add:

- `repo_symbols`
- `repo_imports`
- `repo_env_vars`
- first-pass `repo_routes`
- tools and evals for symbol/import/env/route lookup

This is the next highest-leverage code intelligence layer.

### Phase 3: Delta and Change Impact

Add:

- directory Merkle nodes
- `repo_index_deltas`
- changed symbol/import/route/env detection
- risk flagging
- invalidation candidates

This turns the system from snapshot indexing into longitudinal intelligence.

### Phase 4: Verification Memory

Add:

- `repo_verified_facts`
- `verification_tasks`
- invalidation rules
- agent tool to create targeted questions
- endpoints/UI contract for humans to answer/verify

This is where customer-specific learning becomes durable.

### Phase 5: Queue-Based Infra

Add:

- SQS queue + DLQ
- ECS indexing worker
- RDS/Aurora Postgres with pgvector and pg_trgm
- per-tenant concurrency
- GitHub/OpenAI rate limit handling
- deploy smoke eval

This can run in parallel with Phases 2-4 if the worker starts by calling the
same indexing code paths.

### Phase 6: Quality Flywheel

Add:

- golden dataset expansion
- benchmark reports
- search feedback collection
- agent trace grading
- online metrics dashboard
- regression gates in CI/CD

## Parallel Workstreams

We can move implementation and infrastructure forward in parallel as long as the
contract between them stays small and explicit.

### Workstream A: Code Intelligence

Owned in `hobbesCodebaseAgent`:

- tenant, repo connection, index job, and versioned index schema
- local worker entrypoint that can process one `repo_index_jobs.id`
- static inventory for symbols, imports, routes, env vars, and runtime facts
- exact, semantic, symbol, route, runtime, and verified-fact query tools
- delta and invalidation logic
- evals that prove each index layer improves retrieval or agent outcomes

This workstream should keep running locally without AWS. The first worker can be
called directly from the CLI and later wrapped by the ECS worker loop.

### Workstream B: Cloud Infra

Owned in `HobbesServiceCDK`, preferably as a separate code-indexing stack family
instead of being folded into the existing backend service stack:

- ECR image or reuse existing backend image if the package layout supports it
- ECS/Fargate API service for index/job endpoints
- ECS/Fargate worker service that consumes SQS jobs
- SQS index queue and DLQ
- RDS/Aurora Postgres with `pgvector` and `pg_trgm`
- Secrets Manager entries for OpenAI and GitHub access
- CloudWatch logs, alarms, queue-age metrics, worker health, and job-failure
  metrics
- staging/dev deploy smoke that indexes a small repo and runs retrieval evals

The infra workstream does not need to wait for every intelligence layer. It only
needs the worker contract below.

### Contract Between Code and Infra

The application should expose:

- `GET /health`
- `POST /repo-connections`
- `POST /repo-index-jobs`
- `GET /repo-index-jobs/{job_id}`
- a worker command such as `python -m workers.index_worker`
- a one-shot local command such as `python -m workers.index_one --job-id ...`

The runtime should accept:

- `DATABASE_URL`
- `OPENAI_API_KEY`
- `SQS_INDEX_QUEUE_URL`
- `AWS_REGION`
- `REPO_WORKDIR`
- `WORKER_CONCURRENCY`
- GitHub App credentials or a short-lived Git token source

The worker should guarantee:

- jobs are idempotent
- duplicate SQS delivery is safe
- status transitions are persisted in `repo_index_jobs`
- long jobs extend SQS visibility
- failures include stable `error_code` values
- partial writes are either version-scoped or recoverable

## Immediate Next Work

Recommended next implementation sequence:

1. Add tenant/repo connection/index job schema locally.
2. Add static symbol and import inventory for Python + JS/TS/TSX.
3. Add env var and route extraction for Flask/FastAPI/Next.js.
4. Extend evals to test known symbol, route, env var, and setup answers.
5. Add index delta table and changed-file/symbol/env/route detection.
6. Add verified fact and verification task schema.
7. In parallel, create AWS dev infra for API + SQS + worker + RDS/pgvector.

## References

- Cursor, "Securely indexing large codebases":
  https://cursor.com/blog/secure-codebase-indexing
- Cursor, "Fast regex search: indexing text for agent tools":
  https://cursor.com/blog/fast-regex-search
- Cursor, "Improving agent with semantic search":
  https://cursor.com/blog/semsearch
- Cursor, "Dynamic context discovery":
  https://cursor.com/blog/dynamic-context-discovery
- Cursor, "Continually improving our agent harness":
  https://cursor.com/blog/continually-improving-agent-harness
- Sourcegraph architecture and Zoekt search:
  https://sourcegraph.com/docs/admin/architecture
- Sourcegraph search scaling:
  https://sourcegraph.com/docs/admin/search
- Sourcegraph precise code navigation and SCIP:
  https://sourcegraph.com/docs/code-navigation/precise-code-navigation
- GitHub code navigation:
  https://docs.github.com/en/repositories/working-with-files/using-files/navigating-code-on-github
- GitHub code search launch:
  https://github.blog/news-insights/product-news/github-code-search-is-generally-available/
- Codebase-Memory paper:
  https://arxiv.org/abs/2603.27277
- OpenAI embeddings guide:
  https://developers.openai.com/api/docs/guides/embeddings
- OpenAI agent evals guide:
  https://developers.openai.com/api/docs/guides/agent-evals
- AWS SQS visibility timeout and DLQ guidance:
  https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html
- AWS RDS PostgreSQL extension versions:
  https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-extensions.html
