import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.sandbox_runner import DockerSidecarSandbox


async def main():
    sb = DockerSidecarSandbox(session_id="shtest-" + uuid.uuid4().hex[:6], repo_urls=[])
    await sb.start()
    try:
        r = await sb.run_shell("echo hi && node --version", cwd=None,
                               timeout_seconds=10, max_output_lines=100)
        assert r.exit_code == 0 and "hi" in r.stdout_tail and "v22" in r.stdout_tail, r

        denied = await sb.run_shell("rm -rf /", cwd=None,
                                    timeout_seconds=5, max_output_lines=10)
        assert denied.denied and denied.exit_code == -1, denied

        h = await sb.start_background(
            "python3 -m http.server 9999", cwd="/tmp", name="http",
        )
        await asyncio.sleep(2)
        status = await sb.read_background(h.handle, tail_lines=20)
        assert status["running"], status
        probe = await sb.run_shell(
            "curl -sS -o /dev/null -w '%{http_code}' http://localhost:9999/",
            cwd=None, timeout_seconds=10, max_output_lines=10,
        )
        assert probe.stdout_tail.strip() == "200", probe
        stop = await sb.stop_background(h.handle, grace_seconds=2)
        assert stop["stopped"], stop
    finally:
        await sb.cleanup()
    print("ok")


asyncio.run(main())
