import os
import subprocess
from urllib.parse import urlparse, urlunparse


def _inject_token(repo_url: str, github_token: str) -> str:
    """Return a clone URL with the token embedded as basic auth.
    Only rewrites https GitHub URLs; everything else is passed through unchanged."""
    parsed = urlparse(repo_url)
    if parsed.scheme != "https" or not parsed.netloc.endswith("github.com"):
        return repo_url
    netloc = f"x-access-token:{github_token}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


async def clone_repo(repo_url: str, dest_dir: str, github_token: str | None = None) -> bool:
    clone_url = _inject_token(repo_url, github_token) if github_token else repo_url
    try:
        subprocess.run(
            ["git", "clone", clone_url, dest_dir],
            check=True,
            capture_output=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", errors="replace")
        if github_token:
            stderr = stderr.replace(github_token, "***")
        print(
            f"[clone_repo] git clone failed for {repo_url} "
            f"(token_present={bool(github_token)}): {stderr.strip()}",
            flush=True,
        )
        return False
    except OSError as e:
        print(f"[clone_repo] OSError cloning {repo_url}: {e}", flush=True)
        return False


async def ensure_repo_dir(
    repo_url: str,
    base_dir: str = "/repos",
    github_token: str | None = None,
) -> str | None:
    """Clone the repo if it isn't already on disk. Returns the local path or None on failure.
    Falls back to the GITHUB_TOKEN env var if no token is passed in."""
    repo_name = repo_url.rstrip("/").split("/")[-1].removesuffix(".git")
    repo_dir = os.path.join(base_dir, repo_name)
    if os.path.isdir(repo_dir):
        return repo_dir
    token = github_token or os.environ.get("GITHUB_TOKEN")
    return repo_dir if await clone_repo(repo_url, repo_dir, token) else None
