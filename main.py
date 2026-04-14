import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from temporalio.client import Client
from temporalio.worker import Worker

from activities import onboard_activity
from workflows import OnboardWorkflow
from services.clone_repo import clone_repo
from services.walk_repo import walk_repo


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

@app.get("/walkrepo/{repo_url:path}")
async def walkrepo_endpoint(repo_url: str):
    repo_dir = f"/repos/{uuid.uuid4()}"
    cloned =  await clone_repo(repo_url, repo_dir)
    if(not cloned):
        return {"error": "Failed to clone repository"}
    file_tree = await walk_repo(repo_dir)
    print(file_tree)
    return {"response": file_tree}