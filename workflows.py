from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from activities import (
        WorkflowParams,
        IndexParams,
        AskParams,
        clone_repo_activity,
        index_repo_activity,
        ask_agent_activity,
    )


@workflow.defn
class CodebaseOnboardingWorkflow:
    @workflow.run
    async def run(self, params: WorkflowParams) -> dict:
        repo_dir = await workflow.execute_activity(
            clone_repo_activity,
            params.repo_url,
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        await workflow.execute_activity(
            index_repo_activity,
            IndexParams(repo_url=params.repo_url, repo_dir=repo_dir),
            start_to_close_timeout=timedelta(seconds=300),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )

        return await workflow.execute_activity(
            ask_agent_activity,
            AskParams(
                repo_url=params.repo_url,
                repo_dir=repo_dir,
                query=params.query,
            ),
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
