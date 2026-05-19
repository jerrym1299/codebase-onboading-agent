"""Repair Hobbes repo-demo recipe candidates from failed sandbox runs."""

from __future__ import annotations

import copy
import json
import os
import re
from typing import Any

from openai import OpenAI


RECIPE_REPAIR_MODEL = os.environ.get(
    "RECIPE_REPAIR_MODEL",
    os.environ.get("RECIPE_CANDIDATE_MODEL", os.environ.get("RECIPE_PROPOSAL_MODEL", "gpt-5.4")),
)
VALID_REPAIR_STATUSES = {"repaired", "blocked", "no_change"}


class RecipeRepairError(RuntimeError):
    """Raised when a repair request cannot be processed."""


_openai_client: OpenAI | None = None


def _client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI()
    return _openai_client


async def repair_recipe_candidate(
    repair_bundle: dict[str, Any],
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a structured repair decision for a failed recipe candidate.

    V1 is intentionally model-driven and bounded by the backend repair bundle.
    If the repair model is disabled or unavailable, we return a safe structured
    blocker/no-change result instead of guessing at commands.
    """

    if not isinstance(repair_bundle, dict):
        raise RecipeRepairError("repair_bundle must be an object.")
    if repair_bundle.get("schema_version") != 1:
        raise RecipeRepairError("Unsupported repair_bundle schema_version.")

    fallback = _deterministic_repair_response(repair_bundle)
    llm_disabled = os.environ.get("RECIPE_REPAIR_DISABLE_LLM", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if llm_disabled or not os.environ.get("OPENAI_API_KEY"):
        return fallback

    try:
        response = _call_llm(repair_bundle, metadata=metadata or {})
        return _normalize_repair_response(response, fallback=fallback)
    except Exception as exc:  # Keep backend repair flow non-fatal.
        return {
            **fallback,
            "status": "blocked",
            "change_summary": "Recipe repair model failed before producing a safe revised candidate.",
            "blockers": [
                *list(fallback.get("blockers") or []),
                {
                    "kind": "repair_model_error",
                    "detail": str(exc)[:1000],
                },
            ],
            "model": RECIPE_REPAIR_MODEL,
        }


def _call_llm(repair_bundle: dict[str, Any], *, metadata: dict[str, Any]) -> dict[str, Any]:
    prompt = {
        "repair_bundle": _compact_bundle(repair_bundle),
        "metadata": metadata,
        "output_contract": {
            "status": "repaired|blocked|no_change",
            "revised_candidate": "required only when status=repaired; full candidate payload",
            "change_summary": "short explanation",
            "commands_changed": [],
            "confidence": 0.0,
            "blockers": [],
            "evidence": [],
        },
    }
    response = _client().chat.completions.create(
        model=RECIPE_REPAIR_MODEL,
        temperature=0.1,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an autonomous repo-demo repair agent. You receive a "
                    "failed Hobbes Daytona recipe candidate plus sandbox execution "
                    "evidence. Your job is to decide whether the candidate can be "
                    "safely repaired. Return only JSON. If repaired, return a full "
                    "Hobbes candidate payload with {status, package_manager, config, "
                    "env_template, demo, warnings, evidence, confidence}. Preserve "
                    "known-good fields and change only what the failure evidence "
                    "supports, such as service command, cwd, port, readiness timeout, "
                    "env placeholders, database migrate/seed commands, or demo URL. "
                    "Never invent raw secrets, tokens, private keys, customer data, "
                    "or unsupported scripts. If the evidence is insufficient, return "
                    "blocked or no_change with concrete blockers."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(prompt, sort_keys=True),
            },
        ],
    )
    raw = response.choices[0].message.content or "{}"
    parsed = _parse_json_object(raw)
    usage = response.usage
    parsed.setdefault("model", RECIPE_REPAIR_MODEL)
    parsed.setdefault(
        "usage",
        {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0),
            "completion_tokens": getattr(usage, "completion_tokens", 0),
        },
    )
    return parsed


def _parse_json_object(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("Recipe repair response was not a JSON object.")
    return parsed


def _deterministic_repair_response(repair_bundle: dict[str, Any]) -> dict[str, Any]:
    execution = repair_bundle.get("execution") if isinstance(repair_bundle.get("execution"), dict) else {}
    error = str(execution.get("error") or "").strip()
    command_hint = _failed_command_hint(repair_bundle)
    blockers = []
    if error:
        blockers.append(
            {
                "kind": "verification_failure",
                "detail": error[:1000],
            }
        )
    blockers.append(
        {
            "kind": "agentic_repair_required",
            "detail": (
                "Deterministic repair cannot safely revise startup commands from the "
                "available bundle alone; enable the repair model or sandbox repair loop."
            ),
        }
    )
    return {
        "status": "blocked",
        "revised_candidate": None,
        "change_summary": (
            "Candidate verification failed, but deterministic repair did not have enough "
            "evidence to make a safe command change."
        ),
        "commands_changed": [],
        "confidence": 0.0,
        "blockers": blockers,
        "evidence": [
            item
            for item in [
                {"source": "execution.error", "detail": error[:500]} if error else None,
                {"source": "candidate.command", "detail": command_hint[:500]} if command_hint else None,
            ]
            if item is not None
        ],
        "model": None,
        "usage": {},
    }


def _normalize_repair_response(
    response: dict[str, Any],
    *,
    fallback: dict[str, Any],
) -> dict[str, Any]:
    status = str(response.get("status") or "").strip().lower()
    if status not in VALID_REPAIR_STATUSES:
        return _invalid_agent_output(fallback, "Repair model returned an invalid status.")

    revised_candidate = response.get("revised_candidate")
    if revised_candidate is None and isinstance(response.get("candidate"), dict):
        revised_candidate = response["candidate"]

    if status == "repaired":
        candidate_error = _candidate_payload_error(revised_candidate)
        if candidate_error:
            return _invalid_agent_output(fallback, candidate_error)
        revised_candidate = _normalize_candidate_payload(revised_candidate)
    else:
        revised_candidate = None

    return {
        "status": status,
        "revised_candidate": revised_candidate,
        "change_summary": str(response.get("change_summary") or fallback.get("change_summary") or ""),
        "commands_changed": _string_list(response.get("commands_changed")),
        "confidence": _float_or_none(response.get("confidence")),
        "blockers": _object_list(response.get("blockers")),
        "evidence": _object_list(response.get("evidence")),
        "model": response.get("model"),
        "usage": response.get("usage") if isinstance(response.get("usage"), dict) else {},
    }


def _invalid_agent_output(fallback: dict[str, Any], detail: str) -> dict[str, Any]:
    return {
        **fallback,
        "status": "blocked",
        "change_summary": "Recipe repair model did not return a safe revised candidate.",
        "blockers": [
            *list(fallback.get("blockers") or []),
            {
                "kind": "invalid_repair_output",
                "detail": detail,
            },
        ],
        "model": RECIPE_REPAIR_MODEL,
    }


def _candidate_payload_error(value: Any) -> str | None:
    if not isinstance(value, dict):
        return "Repair model did not return revised_candidate as an object."
    config = value.get("config")
    if not isinstance(config, dict):
        return "Repair model returned revised_candidate without config."
    services = config.get("services")
    if not isinstance(services, dict) or not services:
        return "Repair model returned revised_candidate without config.services."
    for service_name, service in services.items():
        if not isinstance(service, dict):
            return f"Service {service_name!r} is not an object."
        if not str(service.get("command") or "").strip():
            return f"Service {service_name!r} is missing command."
        if service.get("cwd") is not None and not isinstance(service.get("cwd"), str):
            return f"Service {service_name!r} has invalid cwd."
        try:
            int(service.get("port"))
        except (TypeError, ValueError):
            return f"Service {service_name!r} is missing numeric port."
    return None


def _normalize_candidate_payload(candidate: Any) -> dict[str, Any]:
    payload = copy.deepcopy(candidate)
    payload["status"] = str(payload.get("status") or "ok")
    if payload.get("package_manager") not in {"npm", "pnpm", "yarn"}:
        payload["package_manager"] = "npm"
    payload["env_template"] = _placeholder_env_template(payload.get("env_template"))
    if not isinstance(payload.get("demo"), dict):
        payload["demo"] = {}
    if not isinstance(payload.get("warnings"), list):
        payload["warnings"] = []
    if not isinstance(payload.get("evidence"), list):
        payload["evidence"] = []
    payload["confidence"] = _float_or_none(payload.get("confidence"))
    return payload


def _placeholder_env_template(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _placeholder_value(val) for key, val in value.items()}


def _placeholder_value(value: Any) -> str:
    text = "" if value is None else str(value)
    if _looks_sensitive(text):
        return ""
    if len(text) > 120:
        return ""
    return text


def _looks_sensitive(value: str) -> bool:
    return bool(re.search(r"(secret|token|key|password|private|sk-[a-zA-Z0-9])", value, re.IGNORECASE))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _object_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _failed_command_hint(repair_bundle: dict[str, Any]) -> str:
    candidate = repair_bundle.get("candidate") if isinstance(repair_bundle.get("candidate"), dict) else {}
    config = candidate.get("config") if isinstance(candidate.get("config"), dict) else {}
    services = config.get("services") if isinstance(config.get("services"), dict) else {}
    commands = []
    for service_name, service in services.items():
        if isinstance(service, dict) and service.get("command"):
            commands.append(f"{service_name}: {service['command']}")
    return "; ".join(commands)


def _compact_bundle(repair_bundle: dict[str, Any]) -> dict[str, Any]:
    compact = copy.deepcopy(repair_bundle)
    execution = compact.get("execution")
    if isinstance(execution, dict):
        metadata = execution.get("metadata")
        if isinstance(metadata, dict):
            execution["metadata"] = {
                key: value
                for key, value in metadata.items()
                if key in {"stage", "elapsed_seconds", "error", "source", "execution_mode"}
            }
    return compact
