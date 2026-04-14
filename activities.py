from temporalio import activity
from agents import Agent, Runner

agent = Agent(
    name="HR Lady",
    instructions="You are a helpful assistant that can answer questions about the company's policies and procedures. You should always greet the user by their name",
    model="gpt-4o-mini",
)


@activity.defn
async def onboard_activity(name: str) -> str:
    result = await Runner.run(
        agent,
        f"Onboard this new employee, {name} to our company, Lyra technologies.",
    )
    return str(result.final_output)
