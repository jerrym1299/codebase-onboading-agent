import os
import subprocess


async def clone_repo(repo_url: str, dest_dir: str) -> bool:
    try:
        subprocess.run(["git", "clone", repo_url, dest_dir], check=True)
        return True
    except (subprocess.CalledProcessError, OSError):
        return False


async def ensure_repo_dir(repo_url: str, base_dir: str = "/repos") -> str | None:
    """Clone the repo if it isn't already on disk. Returns the local path or None on failure."""
    repo_name = repo_url.rstrip("/").split("/")[-1].removesuffix(".git")
    repo_dir = os.path.join(base_dir, repo_name)
    if os.path.isdir(repo_dir):
        return repo_dir
    return repo_dir if await clone_repo(repo_url, repo_dir) else None
