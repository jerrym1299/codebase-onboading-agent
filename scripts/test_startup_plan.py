"""
test_startup_plan.py — smoke test for the startup-analysis feature.

Creates a session, polls until status == 'ready', fetches the plan,
asserts the structured shape, exercises recompute, and confirms the
Bootstrap agent engages on a 'how do I run this?' message.

Usage:
    python3 scripts/test_startup_plan.py
    python3 scripts/test_startup_plan.py --base http://localhost:8001
    python3 scripts/test_startup_plan.py --repo https://github.com/foo/bar
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_REPO = "https://github.com/ThomasBenjaminCook/WattAppWebApp"


def _post(url: str, body: dict, timeout: int = 60) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body_text)
        except json.JSONDecodeError:
            return exc.code, {"raw": body_text}


def _get(url: str, timeout: int = 60) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body_text)
        except json.JSONDecodeError:
            return exc.code, {"raw": body_text}


def _stream_messages(base: str, session_id: str, content: str,
                     stop_after_seconds: int = 120) -> list[dict]:
    req = urllib.request.Request(
        f"{base}/sessions/{session_id}/messages",
        data=json.dumps({"content": content}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    events: list[dict] = []
    deadline = time.time() + stop_after_seconds
    with urllib.request.urlopen(req, timeout=stop_after_seconds + 5) as resp:
        for raw in resp:
            if time.time() > deadline:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[len("data:"):].strip())
            except json.JSONDecodeError:
                continue
            events.append(event)
            if event.get("type") == "finish":
                break
    return events


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8001")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    args = ap.parse_args()

    print(f"Creating session for {args.repo} ...")
    code, body = _post(f"{args.base}/sessions", {"repo_url": args.repo})
    assert code == 200, f"create session failed: {code} {body}"
    session_id = body["session_id"]
    print(f"  session_id={session_id}")

    print("Polling status ...")
    deadline = time.time() + 240
    status = None
    while time.time() < deadline:
        code, body = _get(f"{args.base}/sessions/{session_id}")
        status = body.get("status")
        print(f"  status={status}")
        if status == "ready":
            break
        if status == "ended":
            sys.exit("Session ended before ready.")
        time.sleep(2)
    if status != "ready":
        sys.exit("Timeout waiting for ready.")

    code, plan_body = _get(f"{args.base}/sessions/{session_id}/startup-plan")
    assert code == 200, f"GET startup-plan failed: {code} {plan_body}"
    plan = plan_body["plan"]
    assert plan_body["analysis_status"] in ("ok", "partial"), \
        f"unexpected analysis_status: {plan_body['analysis_status']}"
    assert plan.get("packages"), "plan.packages is empty"
    pkg = plan["packages"][0]
    assert pkg.get("steps"), f"package has no steps: {pkg}"
    print(f"  plan: status={plan_body['analysis_status']} "
          f"packages={len(plan['packages'])} steps={len(pkg['steps'])}")

    before = plan_body["updated_at"]
    code, _ = _post(f"{args.base}/sessions/{session_id}/startup-plan/recompute", {})
    assert code == 202, f"recompute returned {code}"
    print("  recompute requested; polling updated_at ...")
    advanced = False
    deadline = time.time() + 90
    while time.time() < deadline:
        code, plan_body = _get(f"{args.base}/sessions/{session_id}/startup-plan")
        if plan_body.get("updated_at") != before:
            print(f"  updated_at advanced: {plan_body['updated_at']}")
            advanced = True
            break
        time.sleep(2)
    if not advanced:
        sys.exit("updated_at never advanced after recompute.")

    print("Streaming chat: 'how do I run this repo locally?' ...")
    events = _stream_messages(args.base, session_id, "how do I run this repo locally?",
                              stop_after_seconds=180)
    handoffs = [e for e in events if e.get("type") == "data-handoff"]
    text_parts = [e for e in events if e.get("type") in ("text", "text-delta")]
    print(f"  events={len(events)} handoffs={len(handoffs)} text_chunks={len(text_parts)}")
    bootstrap_seen = any(h.get("agent") == "Bootstrap" for h in handoffs)
    assert bootstrap_seen, f"Bootstrap agent never engaged. handoffs={handoffs}"

    print("\nALL CHECKS PASSED.")


if __name__ == "__main__":
    main()
