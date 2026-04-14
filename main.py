import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from temporalio.client import Client
from temporalio.worker import Worker

from activities import onboard_activity
from workflows import OnboardWorkflow


@asynccontextmanager
async def lifespan(app):
    client = await Client.connect(
        os.environ.get("TEMPORAL_HOST", "temporal:7233")
    )
    app.state.temporal_client = client
    worker = Worker(
        client,
        task_queue="onboarding-queue",
        workflows=[OnboardWorkflow],
        activities=[onboard_activity],
    )
    async with worker:
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
def read_root():
    return {"Hello": "world"}


@app.get("/onboard/{name}")
async def onboard(name: str):
    result = await app.state.temporal_client.execute_workflow(
        OnboardWorkflow.run,
        name,
        id=f"onboard-{name}-{uuid.uuid4()}",
        task_queue="onboarding-queue",
    )
    return {"response": result}
