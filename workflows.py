from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from activities import (
        IndexParams,
        ChatParams,
        SessionStatusParams,
        clone_repo_activity,
        index_repo_activity,
        update_session_status_activity,
    )


@workflow.defn
class CodebaseChatWorkflow:
    def __init__(self) -> None:
        self._ended = False
        self._repo_dir: str | None = None

    @workflow.run
    async def run(self, params: ChatParams) -> dict:
        await workflow.execute_activity(
            update_session_status_activity,
            SessionStatusParams(session_id=params.session_id, status="indexing"),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        self._repo_dir = await workflow.execute_activity(
            clone_repo_activity,
            params.repo_url,
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        await workflow.execute_activity(
            index_repo_activity,
            IndexParams(repo_url=params.repo_url, repo_dir=self._repo_dir),
            start_to_close_timeout=timedelta(seconds=600),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )

        await workflow.execute_activity(
            update_session_status_activity,
            SessionStatusParams(session_id=params.session_id, status="ready"),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        await workflow.wait_condition(lambda: self._ended)

        await workflow.execute_activity(
            update_session_status_activity,
            SessionStatusParams(session_id=params.session_id, status="ended"),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        return {"session_id": params.session_id, "status": "ended"}

    @workflow.signal
    def end(self) -> None:
        self._ended = True
