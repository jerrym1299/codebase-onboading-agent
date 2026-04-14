from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from activities import onboard_activity


@workflow.defn
class OnboardWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            onboard_activity,
            name,
            start_to_close_timeout=timedelta(seconds=60),
        )
