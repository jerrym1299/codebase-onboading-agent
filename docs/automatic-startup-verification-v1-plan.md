# Automatic Startup Verification V1

## Summary

The end-to-end product goal is: a user provides one or more GitHub repos for an application, this backend clones/indexes/analyzes them, produces a startup plan, and then autonomously tries to spin the app up in an isolated sandbox where the program runs. The ideal experience is that the agent can take a repo, start it without errors, and correct the startup plan when it discovers mistakes. A human can help later through chat, but V1 verification should not block on human input.

Add a new Temporal workflow phase after `consolidate_plan_activity` and before the session becomes `ready`: `verify_startup_activity`. This activity runs a bounded verification loop against the consolidated app startup plan. The loop stops as soon as the app is basically working, or when the configured budget is exhausted, whichever happens first.

V1 may update the persisted app-level startup plan markdown, but must not edit cloned repo files. Missing env/auth issues are classified as `blocked`, not `failed`.

## Key Changes

- Extend the app startup plan markdown schema from six to seven required headings by adding `## Verification`.
- Required headings become: `# Startup plan`, `## Prerequisites`, `## Env vars`, `## Steps`, `## Dependency graph`, `## Caveats`, `## Verification`.
- Add structured verification state to `app_startup_plans`: `verification_status` and `verification jsonb`.
- Add `verify_startup_activity` to the Temporal worker and call it in `_run_pipeline()` after consolidation.
- Keep session status non-ready until verification reaches `passed`, `blocked`, or `failed`.
- Add `GET /sessions/:id/startup-verification`.
- Add `POST /sessions/:id/startup-verification/retry`.

## Verification Loop

- The verifier reads the consolidated markdown, repo boundaries, repo files, startup output, and relevant plan context.
- It attempts to run the startup plan commands in order inside a per-session sandbox.
- It observes terminal output, process health, ports, and unauthenticated HTTP probes.
- It designs follow-up probes based on what starts successfully.
- If commands/probes fail, it diagnoses the output and updates the app-level markdown with corrected steps, caveats, ports, prerequisites, or verification notes.
- It then retries using the corrected plan.
- The loop stops when required processes stay alive and expected ports/basic endpoints respond, missing env/auth/out-of-scope requirements block progress, or max iterations/time budget is reached.
- After the loop finishes, always terminate every process started only for verification, then stop/remove the Docker sidecar sandbox, regardless of pass/block/fail outcome.

Default budget: 2 iterations, 20 minutes total. These and related knobs must be env-configurable and documented in README.

## Implementation Changes

- Introduce a `SandboxRunner` abstraction with a Docker sidecar implementation for now, designed so Daytona can replace it later.
- The Docker sidecar gets full outbound network, Docker socket access, and shared repo volume access.
- Make clone storage configurable/mountable so FastAPI and verifier sidecars can read the same cloned repos.
- Track every verification-started foreground/background process handle in the runner.
- Add cleanup in a `finally` path for `verify_startup_activity`: terminate tracked processes, kill any remaining child processes in the sidecar, then remove the sidecar container.
- Reuse/extend the existing `Verifier` agent as the automatic verifier. It should produce both updated schema-valid app markdown and structured verification JSON.
- Persist verification JSON incrementally after each command/probe.
- Rewrite app markdown after each verification iteration and at final result.
- Validate updated markdown against the seven-heading schema. Retry schema repair once; if still invalid, keep the previous plan and record the schema failure in verification JSON.
- Add an obvious-destructive-command denylist. Denied commands produce `blocked`.

## Verification Report Shape

Persist compact JSON in `app_startup_plans.verification`:

- `status`: `not_started|running|passed|blocked|failed`
- `attempts`: bounded list of iteration summaries
- `commands`: command, cwd, exit code, duration, status, redacted stdout/stderr tail
- `probes`: target URL/command, result/status code, pass/fail
- `blockers`: missing env/auth/destructive-command/out-of-scope blockers
- `plan_updates`: summaries of markdown changes made per iteration
- `cleanup`: process/container cleanup actions and outcome
- `final_summary`: concise human-readable outcome
- `updated_at`

## Test Plan

- Unit test seven-heading markdown validation and failed verifier-update rejection.
- Unit test loop stop conditions: pass, blocked, failed by budget.
- Unit test missing env/auth classification as `blocked`.
- Unit test destructive-command denylist.
- Unit test cleanup runs on pass, blocked, failure, timeout, and agent exception.
- Integration smoke test with a tiny sample repo that starts an HTTP server and passes root/health probes.
- Integration failure test where the first plan command is wrong, verifier updates markdown, and retry passes.
- Integration cleanup test verifies no verification sidecar or verification-started process remains after completion.
- API tests for startup verification retrieval, retry, and updated startup plan containing `## Verification`.

## Assumptions

- Verification runs automatically for every new session.
- Verification failure does not fail the Temporal workflow; the session becomes `ready` with `verification_status=failed`.
- V1 is terminal + HTTP only, no browser automation.
- V1 updates app-level markdown only; per-repo startup JSON remains unchanged.
- V1 does not ask humans during automatic verification.
- Full outbound network is allowed in the V1 sandbox.
- Verification sandboxes are disposable; anything running only for verification must be killed after verification completes.
