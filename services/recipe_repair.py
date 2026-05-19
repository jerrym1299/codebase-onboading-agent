"""Repair Hobbes repo-demo recipe candidates from failed sandbox runs."""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any

from openai import OpenAI

from services.clone_repo import ensure_repo_dir
from services.github_app import GitHubAppError, GitHubAppService
from services.recipe_repair_agent import run_sandbox_repair_agent


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

    V1 is model-driven, but first gathers bounded repo observations and builds
    a deterministic repair candidate when package scripts make the fix obvious.
    If the repair model is disabled or unavailable, we return that deterministic
    repair when safe, otherwise a structured blocker instead of guessing.
    """

    if not isinstance(repair_bundle, dict):
        raise RecipeRepairError("repair_bundle must be an object.")
    if repair_bundle.get("schema_version") != 1:
        raise RecipeRepairError("Unsupported repair_bundle schema_version.")

    observations = await _collect_repair_observations(repair_bundle)
    fallback = _deterministic_repair_response(repair_bundle, observations=observations)
    deterministic_repair = _deterministic_repair_from_observations(repair_bundle, observations)
    llm_disabled = os.environ.get("RECIPE_REPAIR_DISABLE_LLM", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if llm_disabled or not os.environ.get("OPENAI_API_KEY"):
        return deterministic_repair or fallback

    if _sandbox_repair_agent_enabled():
        try:
            agent_response = await run_sandbox_repair_agent(
                repair_bundle,
                metadata=metadata or {},
                observations=observations,
                deterministic_repair=deterministic_repair,
            )
            normalized = _normalize_repair_response(agent_response, fallback=fallback)
            if _invalid_repair_output(normalized) and deterministic_repair is not None:
                return deterministic_repair
            return normalized
        except Exception as exc:
            return {
                **fallback,
                "status": "blocked",
                "change_summary": "Sandbox repair agent failed before producing a safe revised candidate.",
                "blockers": [
                    *list(fallback.get("blockers") or []),
                    {
                        "kind": "sandbox_repair_agent_error",
                        "detail": str(exc)[:1000],
                    },
                ],
                "model": RECIPE_REPAIR_MODEL,
                "repair_strategy": None,
                "repair_transcript": [],
            }

    try:
        response = _call_llm(
            repair_bundle,
            metadata=metadata or {},
            observations=observations,
            deterministic_repair=deterministic_repair,
        )
        normalized = _normalize_repair_response(response, fallback=fallback)
        if normalized["status"] == "blocked" and deterministic_repair is not None:
            return deterministic_repair
        return normalized
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


def _call_llm(
    repair_bundle: dict[str, Any],
    *,
    metadata: dict[str, Any],
    observations: dict[str, Any],
    deterministic_repair: dict[str, Any] | None,
) -> dict[str, Any]:
    prompt = {
        "repair_bundle": _compact_bundle(repair_bundle),
        "metadata": metadata,
        "repair_observations": observations,
        "deterministic_repair_candidate": deterministic_repair,
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
                    "evidence plus bounded repo observations from package manifests "
                    "and candidate configuration. Your job is to decide whether the "
                    "candidate can be safely repaired. Return only JSON. If repaired, "
                    "return a full Hobbes candidate payload with {status, "
                    "package_manager, config, env_template, demo, warnings, evidence, "
                    "confidence}. Preserve known-good fields and change only what the "
                    "failure evidence supports, such as service command, cwd, port, "
                    "readiness timeout, env placeholders, database migrate/seed "
                    "commands, or demo URL. Never invent raw secrets, tokens, private "
                    "keys, customer data, or unsupported scripts. If the evidence is "
                    "insufficient, return blocked or no_change with concrete blockers."
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


async def _collect_repair_observations(repair_bundle: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("RECIPE_REPAIR_DISABLE_INSPECTION", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return {"enabled": False, "reason": "RECIPE_REPAIR_DISABLE_INSPECTION is enabled."}

    repo_context = repair_bundle.get("repo_context") if isinstance(repair_bundle.get("repo_context"), dict) else {}
    repo_url = str(repo_context.get("repo_url") or "").strip()
    if not repo_url:
        return {"enabled": False, "reason": "repair bundle did not include repo_context.repo_url."}

    token = await _github_clone_token(repo_context)
    base_dir = os.environ.get("REPO_WORKDIR", "/repos")
    try:
        Path(base_dir).mkdir(parents=True, exist_ok=True)
        repo_dir = await ensure_repo_dir(repo_url, base_dir=base_dir, github_token=token)
    except OSError as exc:
        return {
            "enabled": True,
            "repo_url": _safe_repo_url(repo_url),
            "clone_status": "failed",
            "error": str(exc)[:1000],
        }
    if repo_dir is None:
        return {
            "enabled": True,
            "repo_url": _safe_repo_url(repo_url),
            "clone_status": "failed",
            "error": "failed to clone or resolve repository",
        }

    repo_path = Path(repo_dir)
    package_manifests = _collect_package_manifests(repo_path)
    service_observations = _observe_candidate_services(
        repair_bundle,
        package_manifests=package_manifests,
    )
    return {
        "enabled": True,
        "repo_url": _safe_repo_url(repo_url),
        "repo_dir": str(repo_path),
        "clone_status": "available",
        "package_manager": _detect_package_manager(repo_path),
        "package_manifests": package_manifests[:20],
        "services": service_observations,
    }


async def _github_clone_token(repo_context: dict[str, Any]) -> str | None:
    installation_id = repo_context.get("github_installation_id")
    if not installation_id:
        return None
    try:
        return (
            await GitHubAppService().create_installation_access_token(
                installation_id,
                repository_id=repo_context.get("github_repository_id"),
            )
        ).token
    except GitHubAppError:
        return None


def _collect_package_manifests(repo_path: Path) -> list[dict[str, Any]]:
    manifests: list[dict[str, Any]] = []
    max_files = max(1, min(_int_or_default(os.environ.get("RECIPE_REPAIR_MAX_PACKAGE_MANIFESTS"), 40), 200))
    ignored_parts = {"node_modules", ".git", ".next", "dist", "build", "coverage"}
    for package_path in sorted(repo_path.rglob("package.json")):
        if any(part in ignored_parts for part in package_path.relative_to(repo_path).parts):
            continue
        try:
            parsed = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(parsed, dict):
            continue
        rel_path = package_path.relative_to(repo_path).as_posix()
        rel_dir = "" if rel_path == "package.json" else rel_path.rsplit("/", 1)[0]
        scripts = parsed.get("scripts") if isinstance(parsed.get("scripts"), dict) else {}
        deps: dict[str, Any] = {}
        for key in ("dependencies", "devDependencies"):
            value = parsed.get(key)
            if isinstance(value, dict):
                deps.update(value)
        manifests.append(
            {
                "path": rel_path,
                "cwd": rel_dir,
                "name": parsed.get("name"),
                "scripts": {str(k): str(v) for k, v in scripts.items()},
                "dependency_keys": sorted(str(key) for key in deps.keys())[:80],
                "framework_signals": _framework_signals(deps),
            }
        )
        if len(manifests) >= max_files:
            break
    return manifests


def _observe_candidate_services(
    repair_bundle: dict[str, Any],
    *,
    package_manifests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    services = _candidate_services(repair_bundle)
    observations: list[dict[str, Any]] = []
    for service_name, service in services.items():
        if not isinstance(service, dict):
            continue
        cwd = str(service.get("cwd") or "")
        command = str(service.get("command") or "")
        package = _manifest_for_cwd(package_manifests, cwd) or _best_manifest_for_service(
            package_manifests,
            service_name=service_name,
        )
        script = _script_from_command(command)
        package_scripts = package.get("scripts") if isinstance(package, dict) else {}
        observations.append(
            {
                "service": str(service_name),
                "cwd": cwd,
                "command": command,
                "port": service.get("port"),
                "command_script": script,
                "package_json_found": package is not None,
                "package_json_path": package.get("path") if isinstance(package, dict) else None,
                "available_scripts": sorted(package_scripts.keys()) if isinstance(package_scripts, dict) else [],
                "script_exists": bool(script and isinstance(package_scripts, dict) and script in package_scripts),
                "framework_signals": package.get("framework_signals") if isinstance(package, dict) else [],
                "recommended_script": _recommended_script(package, service_name=str(service_name)),
            }
        )
    return observations


def _deterministic_repair_from_observations(
    repair_bundle: dict[str, Any],
    observations: dict[str, Any],
) -> dict[str, Any] | None:
    if not observations.get("enabled") or observations.get("clone_status") != "available":
        return None

    candidate_payload = _candidate_payload_from_bundle(repair_bundle)
    services = candidate_payload.get("config", {}).get("services", {})
    if not isinstance(services, dict) or not services:
        return None

    changed: list[str] = []
    evidence: list[dict[str, Any]] = []
    package_manager = str(observations.get("package_manager") or candidate_payload.get("package_manager") or "npm")
    candidate_payload["package_manager"] = package_manager

    for service_observation in observations.get("services") or []:
        service_name = str(service_observation.get("service") or "")
        service = services.get(service_name)
        if not isinstance(service, dict):
            continue
        recommended = service_observation.get("recommended_script")
        if not isinstance(recommended, dict):
            continue
        current_script = service_observation.get("command_script")
        script_exists = bool(service_observation.get("script_exists"))
        should_repair = (
            not current_script
            or not script_exists
            or _failure_suggests_bad_command(repair_bundle)
        )
        if not should_repair:
            continue

        port = _int_or_default(
            service.get("port") or service_observation.get("port"),
            _default_port(service_name),
        )
        new_command = _command_for_script(
            package_manager=package_manager,
            script=str(recommended["script"]),
            port=port,
            framework_signals=service_observation.get("framework_signals") or [],
            service_name=service_name,
        )
        new_cwd = str(recommended.get("cwd") or service.get("cwd") or "")
        if service.get("command") != new_command:
            service["command"] = new_command
            changed.append(f"{service_name}.command")
        if service.get("cwd") != new_cwd:
            service["cwd"] = new_cwd
            changed.append(f"{service_name}.cwd")
        service["port"] = port
        if service_name == "frontend":
            service.setdefault("primary", True)
            service.setdefault("preview", True)
        evidence.append(
            {
                "path": recommended.get("path"),
                "reason": f"Selected {recommended['script']!r} script for {service_name} repair.",
            }
        )

    if not changed:
        return None

    candidate_payload.setdefault("warnings", [])
    if isinstance(candidate_payload["warnings"], list):
        candidate_payload["warnings"].append(
            "Candidate was repaired from package manifest observations; rerun sandbox verification."
        )
    candidate_payload["status"] = "ok"
    candidate_payload["evidence"] = [
        *list(candidate_payload.get("evidence") or []),
        *evidence,
    ]
    candidate_payload["confidence"] = max(_float_or_none(candidate_payload.get("confidence")) or 0.0, 0.72)

    return {
        "status": "repaired",
        "revised_candidate": _normalize_candidate_payload(candidate_payload),
        "change_summary": "Repaired candidate startup service commands from package manifest observations.",
        "commands_changed": changed,
        "confidence": candidate_payload["confidence"],
        "blockers": [],
        "evidence": evidence,
        "model": None,
        "usage": {},
    }


def _deterministic_repair_response(
    repair_bundle: dict[str, Any],
    *,
    observations: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
                "available bundle alone; enable the repair model or provide package "
                "manifest observations."
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
                {"source": "repair_observations", "detail": str(observations)[:500]}
                if observations
                else None,
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

    normalized = {
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
    for key in ("repair_strategy", "repair_transcript", "candidate_diff"):
        if key in response:
            normalized[key] = response[key]
    return normalized


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


def _invalid_repair_output(response: dict[str, Any]) -> bool:
    return any(
        isinstance(blocker, dict) and blocker.get("kind") == "invalid_repair_output"
        for blocker in response.get("blockers") or []
    )


def _sandbox_repair_agent_enabled() -> bool:
    return os.environ.get("RECIPE_REPAIR_ENABLE_SANDBOX_AGENT", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
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


def _candidate_payload_from_bundle(repair_bundle: dict[str, Any]) -> dict[str, Any]:
    candidate = repair_bundle.get("candidate") if isinstance(repair_bundle.get("candidate"), dict) else {}
    config = copy.deepcopy(candidate.get("config") if isinstance(candidate.get("config"), dict) else {})
    env_template = _placeholder_env_template(candidate.get("env_template"))
    for key in candidate.get("env_template_keys") or []:
        env_template.setdefault(str(key), "")
    env_keys = config.pop("env_var_keys", None)
    if isinstance(env_keys, list):
        for key in env_keys:
            env_template.setdefault(str(key), "")
        config["env"] = {str(key): env_template.get(str(key), "") for key in env_keys}
    config.setdefault("env", {})
    return {
        "status": candidate.get("status") or "partial",
        "package_manager": candidate.get("package_manager") or "npm",
        "config": config,
        "env_template": env_template,
        "demo": copy.deepcopy(candidate.get("demo") if isinstance(candidate.get("demo"), dict) else {}),
        "warnings": copy.deepcopy(candidate.get("warnings") if isinstance(candidate.get("warnings"), list) else []),
        "evidence": copy.deepcopy(candidate.get("evidence") if isinstance(candidate.get("evidence"), list) else []),
        "confidence": candidate.get("confidence"),
    }


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


def _candidate_services(repair_bundle: dict[str, Any]) -> dict[str, Any]:
    candidate = repair_bundle.get("candidate") if isinstance(repair_bundle.get("candidate"), dict) else {}
    config = candidate.get("config") if isinstance(candidate.get("config"), dict) else {}
    services = config.get("services") if isinstance(config.get("services"), dict) else {}
    return services


def _manifest_for_cwd(package_manifests: list[dict[str, Any]], cwd: str) -> dict[str, Any] | None:
    normalized = cwd.strip("/.")
    for manifest in package_manifests:
        manifest_cwd = str(manifest.get("cwd") or "").strip("/.")
        if manifest_cwd == normalized:
            return manifest
    if not normalized:
        for manifest in package_manifests:
            if not str(manifest.get("cwd") or ""):
                return manifest
    return None


def _best_manifest_for_service(
    package_manifests: list[dict[str, Any]],
    *,
    service_name: str,
) -> dict[str, Any] | None:
    if not package_manifests:
        return None
    if len(package_manifests) == 1:
        return package_manifests[0]
    is_frontend = service_name == "frontend"
    scored: list[tuple[int, dict[str, Any]]] = []
    for manifest in package_manifests:
        signals = set(manifest.get("framework_signals") or [])
        cwd = str(manifest.get("cwd") or "")
        scripts = manifest.get("scripts") if isinstance(manifest.get("scripts"), dict) else {}
        score = 0
        if is_frontend and signals.intersection({"vite", "next", "react", "vue", "svelte"}):
            score += 5
        if not is_frontend and signals.intersection({"express", "fastify", "nestjs", "hono", "prisma"}):
            score += 5
        if "dev" in scripts or "start" in scripts:
            score += 2
        if is_frontend and cwd in {"", "web", "frontend", "client", "apps/web", "apps/frontend"}:
            score += 2
        if not is_frontend and cwd in {"api", "backend", "server", "apps/api", "apps/backend"}:
            score += 2
        scored.append((score, manifest))
    return max(scored, key=lambda item: item[0])[1]


def _recommended_script(package: dict[str, Any] | None, *, service_name: str) -> dict[str, str] | None:
    if not isinstance(package, dict):
        return None
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    preferred = ("dev", "start") if service_name == "frontend" else ("dev", "start", "serve")
    for script in preferred:
        if script in scripts:
            return {"script": script, "cwd": str(package.get("cwd") or ""), "path": str(package.get("path") or "")}
    return None


def _script_from_command(command: str) -> str | None:
    match = re.search(r"\b(?:npm|pnpm)\s+(?:run\s+)?([A-Za-z0-9:_-]+)", command)
    if match:
        return match.group(1)
    match = re.search(r"\byarn\s+(?:run\s+)?([A-Za-z0-9:_-]+)", command)
    if match:
        script = match.group(1)
        return None if script in {"install", "add", "remove"} else script
    return None


def _command_for_script(
    *,
    package_manager: str,
    script: str,
    port: int,
    framework_signals: list[str],
    service_name: str,
) -> str:
    if package_manager == "yarn":
        command = f"yarn {script}"
    else:
        command = f"{package_manager} run {script}"
    signals = set(framework_signals)
    if service_name == "frontend":
        if "vite" in signals:
            return f"{command} -- --host 0.0.0.0 --port {port} --strictPort"
        if "next" in signals:
            return f"{command} -- -H 0.0.0.0 -p {port}"
    return command


def _detect_package_manager(repo_path: Path) -> str:
    if (repo_path / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (repo_path / "yarn.lock").exists():
        return "yarn"
    if any(repo_path.glob("**/pnpm-lock.yaml")):
        return "pnpm"
    if any(repo_path.glob("**/yarn.lock")):
        return "yarn"
    return "npm"


def _framework_signals(deps: dict[str, Any]) -> list[str]:
    mapping = {
        "vite": "vite",
        "next": "next",
        "react": "react",
        "vue": "vue",
        "svelte": "svelte",
        "express": "express",
        "fastify": "fastify",
        "hono": "hono",
        "@nestjs/core": "nestjs",
        "prisma": "prisma",
        "@prisma/client": "prisma",
    }
    return [signal for dep, signal in mapping.items() if dep in deps]


def _failure_suggests_bad_command(repair_bundle: dict[str, Any]) -> bool:
    execution = repair_bundle.get("execution") if isinstance(repair_bundle.get("execution"), dict) else {}
    error = str(execution.get("error") or "").lower()
    return any(
        needle in error
        for needle in (
            "cannot find module",
            "missing script",
            "no such file",
            "not found",
            "never started listening",
            "command failed",
        )
    )


def _default_port(service_name: str) -> int:
    return 5173 if service_name == "frontend" else 8000


def _int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_repo_url(repo_url: str) -> str:
    return re.sub(r"://[^/@]+@", "://***@", repo_url)


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
