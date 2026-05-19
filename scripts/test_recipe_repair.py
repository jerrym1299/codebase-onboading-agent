import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["RECIPE_REPAIR_DISABLE_LLM"] = "true"

from services.recipe_repair import (  # noqa: E402
    _normalize_repair_response,
    repair_recipe_candidate,
)


BUNDLE = {
    "schema_version": 1,
    "status": "repair_ready",
    "recipe_id": "recipe-123",
    "organization_id": "org-123",
    "candidate_id": "candidate-123",
    "candidate_version": "candidate-123",
    "repo_context": {
        "repo_url": "https://github.com/example/widget.git",
        "repo_full_name": "example/widget",
        "branch": "main",
        "repo_index_id": "index-123",
    },
    "candidate": {
        "status": "ok",
        "source": "openai_recipe_agent",
        "model": "gpt-5.4",
        "confidence": 0.74,
        "package_manager": "npm",
        "config": {
            "services": {
                "frontend": {
                    "command": "npm run dev -- --host 0.0.0.0 --port 5173 --strictPort",
                    "cwd": "",
                    "port": 5173,
                    "primary": True,
                    "preview": True,
                    "readinessTimeoutSeconds": 240,
                    "env_var_keys": [],
                }
            },
            "env_var_keys": ["NODE_ENV"],
            "demo": {"loginUrl": "http://localhost:5173"},
        },
        "env_template_keys": ["NODE_ENV", "SECRET_TOKEN"],
        "demo": {"loginUrl": "http://localhost:5173"},
        "warnings": [],
        "evidence": [{"path": "package.json", "reason": "script evidence"}],
    },
    "execution": {
        "sandbox_run_id": "run-123",
        "status": "failed",
        "stage": "candidate_verification_failed",
        "error": "Service frontend never started listening on port 5173",
        "metadata": {"stage": "candidate_verification_failed"},
    },
    "repair_history": [],
    "constraints": {
        "max_repair_attempts": 3,
        "attempt_count": 0,
        "secret_policy": "Do not include raw secrets.",
    },
}


async def main() -> None:
    blocked = await repair_recipe_candidate(BUNDLE)
    assert blocked["status"] == "blocked"
    assert blocked["revised_candidate"] is None
    assert blocked["blockers"]
    assert blocked["evidence"]

    repaired_candidate = {
        "status": "ok",
        "package_manager": "pnpm",
        "config": {
            "services": {
                "frontend": {
                    "command": "pnpm dev --host 0.0.0.0 --port 5173",
                    "cwd": "",
                    "port": 5173,
                    "primary": True,
                    "preview": True,
                }
            }
        },
        "env_template": {
            "NODE_ENV": "development",
            "SECRET_TOKEN": "sk-test-should-redact",
        },
        "demo": {"loginUrl": "http://localhost:5173"},
        "warnings": [],
        "evidence": [{"path": "package.json", "reason": "pnpm dev script"}],
        "confidence": "0.82",
    }
    normalized = _normalize_repair_response(
        {
            "status": "repaired",
            "revised_candidate": repaired_candidate,
            "change_summary": "Switch frontend to pnpm dev.",
            "commands_changed": ["frontend.command"],
            "confidence": 0.82,
            "blockers": [],
            "evidence": [{"path": "package.json"}],
            "model": "test-model",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
        fallback=blocked,
    )
    assert normalized["status"] == "repaired"
    assert normalized["revised_candidate"]["config"]["services"]["frontend"]["command"].startswith("pnpm dev")
    assert normalized["revised_candidate"]["env_template"]["SECRET_TOKEN"] == ""
    assert normalized["commands_changed"] == ["frontend.command"]

    invalid = _normalize_repair_response(
        {"status": "repaired", "revised_candidate": {"config": {"services": {}}}},
        fallback=blocked,
    )
    assert invalid["status"] == "blocked"
    assert any(b["kind"] == "invalid_repair_output" for b in invalid["blockers"])

    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
