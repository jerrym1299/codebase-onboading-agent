import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.sandbox_runner import DockerSidecarSandbox


async def main():
    sb = DockerSidecarSandbox(
        session_id=str(uuid.uuid4())[:8],
        repo_urls=["https://github.com/sindresorhus/p-map"],
    )
    await sb.start()
    rc, out, err = await sb._run_host(
        ["docker", "exec", sb.container_name, "ls", "/repos"], timeout=10,
    )
    assert rc == 0 and "p-map" in out, (rc, out, err)
    result = await sb.cleanup()
    assert result["sidecar_removed"], result
    print("ok")


asyncio.run(main())
