import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from activities import (
        CloneParams,
        IndexParams,
        ChatParams,
        SessionStatusParams,
        AgentTurnParams,
        AnalyzeStartupParams,
        ExtractBoundariesParams,
        BuildGraphParams,
        ConsolidateParams,
        PipelineFailedParams,
        clone_repo_activity,
        index_repo_activity,
        update_session_status_activity,
        agent_turn_activity,
        analyze_startup_activity,
        extract_boundaries_activity,
        build_graph_activity,
        consolidate_plan_activity,
        publish_pipeline_failed_activity,
        cancel_pending_actions_activity,
        resolve_pending_actions_activity,
    )


@workflow.defn
class CodebaseChatWorkflow:
    def __init__(self) -> None:
        self._ended = False
        self._status: str = "starting"
        self._repo_dirs: dict[str, str] = {}
        self._repo_urls: list[str] = []
        self._repo_set_hash: str = ""
        self._session_id: str | None = None
        self._user_messages: list[str] = []
        self._clarifications: list[tuple[str, dict]] = []
        self._pending: dict[str, dict] = {}
        self._recompute_requested = False
        self._recompute_reason = ""

    async def _run_pipeline(self, session_id: str, force: bool) -> None:
        """Per-repo (clone/index/analyze/extract) in parallel, then matcher,
        then consolidator. Used both on initial start and on recompute."""
        async def per_repo(repo_url: str) -> tuple[str, str]:
            repo_dir = await workflow.execute_activity(
                clone_repo_activity,
                CloneParams(repo_url=repo_url, session_id=session_id),
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            await workflow.execute_activity(
                index_repo_activity,
                IndexParams(repo_url=repo_url, repo_dir=repo_dir, session_id=session_id),
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            await workflow.execute_activity(
                analyze_startup_activity,
                AnalyzeStartupParams(
                    session_id=session_id,
                    repo_url=repo_url,
                    repo_dir=repo_dir,
                    force=force,
                ),
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            await workflow.execute_activity(
                extract_boundaries_activity,
                ExtractBoundariesParams(
                    session_id=session_id,
                    repo_url=repo_url,
                    repo_dir=repo_dir,
                ),
                start_to_close_timeout=timedelta(seconds=240),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            return repo_url, repo_dir

        results = await asyncio.gather(*[per_repo(u) for u in self._repo_urls])
        self._repo_dirs = dict(results)

        await workflow.execute_activity(
            build_graph_activity,
            BuildGraphParams(
                session_id=session_id,
                repo_set_hash=self._repo_set_hash,
                repo_urls=self._repo_urls,
                repo_dirs=self._repo_dirs,
            ),
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )

        await workflow.execute_activity(
            consolidate_plan_activity,
            ConsolidateParams(
                session_id=session_id,
                repo_set_hash=self._repo_set_hash,
                repo_urls=self._repo_urls,
                repo_dirs=self._repo_dirs,
            ),
            start_to_close_timeout=timedelta(seconds=600),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

    async def _emit_pipeline_failed(
        self, session_id: str, phase: str, exc: BaseException
    ) -> None:
        await workflow.execute_activity(
            publish_pipeline_failed_activity,
            PipelineFailedParams(
                session_id=session_id, phase=phase, message=str(exc),
            ),
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

    @workflow.run
    async def run(self, params: ChatParams) -> dict:
        self._repo_urls = list(params.repo_urls)
        self._repo_set_hash = params.repo_set_hash
        self._session_id = params.session_id
        self._status = "indexing"
        await workflow.execute_activity(
            update_session_status_activity,
            SessionStatusParams(session_id=params.session_id, status="indexing"),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        try:
            await self._run_pipeline(params.session_id, force=False)
        except Exception as exc:
            workflow.logger.exception("initial pipeline failed: %s", exc)
            await self._emit_pipeline_failed(
                params.session_id, "initial_pipeline", exc,
            )
            self._status = "failed"
            await workflow.execute_activity(
                update_session_status_activity,
                SessionStatusParams(
                    session_id=params.session_id, status="failed",
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            return {"session_id": params.session_id, "status": "failed"}

        self._status = "ready"
        await workflow.execute_activity(
            update_session_status_activity,
            SessionStatusParams(session_id=params.session_id, status="ready"),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        while not self._ended:
            await workflow.wait_condition(
                lambda: bool(self._user_messages)
                        or bool(self._clarifications)
                        or self._recompute_requested
                        or self._ended
            )
            if self._ended:
                break

            if self._recompute_requested:
                self._recompute_requested = False
                workflow.logger.info(
                    "recompute_startup_plan signal: reason=%r", self._recompute_reason,
                )
                self._recompute_reason = ""
                self._status = "indexing"
                await workflow.execute_activity(
                    update_session_status_activity,
                    SessionStatusParams(session_id=params.session_id, status="indexing"),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                try:
                    await self._run_pipeline(params.session_id, force=True)
                except Exception as exc:
                    workflow.logger.exception("recompute pipeline failed: %s", exc)
                    await self._emit_pipeline_failed(
                        params.session_id, "recompute_pipeline", exc,
                    )
                self._status = "ready"
                await workflow.execute_activity(
                    update_session_status_activity,
                    SessionStatusParams(session_id=params.session_id, status="ready"),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                continue

            if self._user_messages:
                content = self._user_messages.pop(0)

                if self._pending:
                    self._pending.clear()
                    await workflow.execute_activity(
                        resolve_pending_actions_activity,
                        params.session_id,
                        start_to_close_timeout=timedelta(seconds=15),
                        retry_policy=RetryPolicy(maximum_attempts=3),
                    )

                result = await workflow.execute_activity(
                    agent_turn_activity,
                    AgentTurnParams(session_id=params.session_id, content=content),
                    start_to_close_timeout=timedelta(seconds=300),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
                if result.get("kind") == "paused":
                    self._pending[result["pending_id"]] = result.get("payload", {})

        await workflow.execute_activity(
            cancel_pending_actions_activity,
            params.session_id,
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        self._status = "ended"
        await workflow.execute_activity(
            update_session_status_activity,
            SessionStatusParams(session_id=params.session_id, status="ended"),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        return {"session_id": params.session_id, "status": "ended"}

    @workflow.signal
    def user_message(self, content: str) -> None:
        self._user_messages.append(content)

    @workflow.signal
    def clarification_response(self, pending_id: str, value: dict) -> None:
        self._clarifications.append((pending_id, value))
        self._pending.pop(pending_id, None)

    @workflow.signal
    def end_session(self) -> None:
        self._ended = True

    @workflow.signal
    def recompute_startup_plan(self, reason: str = "") -> None:
        self._recompute_requested = True
        self._recompute_reason = reason

    @workflow.query
    def get_status(self) -> str:
        return self._status

    @workflow.query
    def get_pending(self) -> list[dict]:
        return [{"id": pid, **payload} for pid, payload in self._pending.items()]
