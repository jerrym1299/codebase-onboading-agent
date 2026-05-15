import os
import subprocess
from urllib.parse import urlparse

from services.clone_repo import ensure_repo_dir
from services.db import get_repo_index_job, update_repo_index_job_status
from services.github_app import GitHubAppError, GitHubAppService
from services.indexing import index_repo_path


def _is_github_repo(repo_url: str) -> bool:
    parsed = urlparse(repo_url)
    return parsed.netloc.endswith("github.com")


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


async def _github_clone_token_for_job(job: dict) -> str | None:
    if not _is_github_repo(job["repo_url"]):
        return None

    metadata = job.get("repo_connection_metadata") or {}
    installation_id = job.get("installation_id") or metadata.get("github_installation_id")
    if not installation_id:
        return None

    repository_id = metadata.get("github_repository_id")
    try:
        token = await GitHubAppService().create_installation_access_token(
            installation_id,
            repository_id=repository_id,
        )
    except GitHubAppError as exc:
        raise RuntimeError(
            f"Failed to mint GitHub App installation token for installation {installation_id}"
        ) from exc
    return token.token


async def process_repo_index_job(
    job_id: str,
    *,
    generate_summaries: bool = True,
) -> dict:
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
        github_token = await _github_clone_token_for_job(job)
        repo_dir = await ensure_repo_dir(
            job["repo_url"],
            base_dir=os.environ.get("REPO_WORKDIR", "/repos"),
            github_token=github_token,
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
            generate_summaries=generate_summaries,
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
