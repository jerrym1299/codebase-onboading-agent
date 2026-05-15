"""Minimal code indexing control-plane API.

This entrypoint intentionally avoids Temporal and agent chat dependencies so it
can run as a small cloud-facing service for job submission/status checks.
"""

from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse

from services.db import (
    close_pool,
    create_repo_index_job,
    ensure_repo_connection,
    get_repo_index_job,
    init_schema,
)


API_KEY_HEADER = "X-Hobbes-Code-Indexing-Key"


def _configured_api_key() -> str | None:
    value = os.getenv("CODE_INDEXING_API_KEY", "").strip()
    return value or None


async def require_api_key(
    x_hobbes_code_indexing_key: Annotated[str | None, Header(alias=API_KEY_HEADER)] = None,
) -> None:
    expected = _configured_api_key()
    if expected is None:
        return
    provided = (x_hobbes_code_indexing_key or "").strip()
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid code indexing API key.",
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_schema()
    try:
        yield
    finally:
        await close_pool()


app = FastAPI(title="Hobbes Code Indexing API", lifespan=lifespan)


@app.get("/")
def read_root():
    return {"service": "hobbes-code-indexing", "status": "ok"}


@app.get("/health")
def health_endpoint():
    return {"status": "ok"}


@app.post("/repo-connections")
async def create_repo_connection_endpoint(
    payload: dict,
    _auth: None = Depends(require_api_key),
):
    repo_url = (payload or {}).get("repo_url", "").rstrip("/")
    if not repo_url:
        return JSONResponse(status_code=400, content={"error": "Missing 'repo_url'."})
    metadata = (payload or {}).get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        return JSONResponse(status_code=400, content={"error": "'metadata' must be an object."})
    connection = await ensure_repo_connection(
        repo_url,
        tenant_id=(payload or {}).get("tenant_id"),
        provider=(payload or {}).get("provider"),
        default_branch=(payload or {}).get("default_branch"),
        installation_id=(payload or {}).get("installation_id"),
        metadata=metadata,
    )
    return connection


@app.post("/repo-index-jobs")
async def create_repo_index_job_endpoint(
    payload: dict,
    _auth: None = Depends(require_api_key),
):
    payload = payload or {}
    try:
        priority = int(payload.get("priority") or 100)
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "'priority' must be an integer."})

    try:
        job = await create_repo_index_job(
            repo_url=(payload.get("repo_url") or "").rstrip("/") or None,
            repo_connection_id=payload.get("repo_connection_id"),
            tenant_id=payload.get("tenant_id"),
            provider=payload.get("provider"),
            default_branch=payload.get("default_branch"),
            installation_id=payload.get("installation_id"),
            requested_by=payload.get("requested_by"),
            trigger=payload.get("trigger") or "manual",
            target_ref=payload.get("target_ref") or "HEAD",
            target_commit_sha=payload.get("target_commit_sha"),
            priority=priority,
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return JSONResponse(status_code=202, content=job)


@app.get("/repo-index-jobs/{job_id}")
async def get_repo_index_job_endpoint(
    job_id: str,
    _auth: None = Depends(require_api_key),
):
    job = await get_repo_index_job(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"error": "Job not found."})
    return job
