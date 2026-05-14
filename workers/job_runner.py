import os
import subprocess

from services.clone_repo import ensure_repo_dir
from services.db import get_repo_index_job, update_repo_index_job_status
from services.indexing import index_repo_path


def _git_output(repo_dir: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", repo_dir, *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


async def process_repo_index_job(job_id: str) -> dict:
    job = await get_repo_index_job(job_id)
    if job is None:
        raise ValueError(f"Unknown repo_index_job: {job_id}")
    if job["status"] == "complete":
        return job

    try:
        await update_repo_index_job_status(
            job_id,
            "cloning",
            increment_attempt=job["status"] != "cloning",
        )
        repo_dir = await ensure_repo_dir(
            job["repo_url"],
            base_dir=os.environ.get("REPO_WORKDIR", "/repos"),
        )
        if repo_dir is None:
            raise RuntimeError(f"Failed to clone {job['repo_url']}")

        target_ref = job["target_ref"] or "HEAD"
        commit_sha = job["target_commit_sha"] or _git_output(
            repo_dir,
            "rev-parse",
            target_ref,
        )
        branch = _git_output(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")

        result = await index_repo_path(
            repo_url=job["repo_url"],
            repo_dir=repo_dir,
            source="repo_index_job",
            tenant_id=job["tenant_id"],
            repo_connection_id=job["repo_connection_id"],
            job_id=job_id,
            commit_sha=commit_sha,
            branch=branch,
            generate_summaries=True,
        )
        updated = await update_repo_index_job_status(
            job_id,
            "complete",
            repo_index_id=result["repo_index_id"],
            metrics=result,
        )
        if updated is None:
            raise RuntimeError(f"Repo index job disappeared after completion: {job_id}")
        return updated
    except Exception as exc:
        await update_repo_index_job_status(
            job_id,
            "failed",
            error_code=exc.__class__.__name__,
            error_message=str(exc),
        )
        raise
