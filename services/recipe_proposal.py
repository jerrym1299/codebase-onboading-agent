"""Generate a Hobbes Daytona recipe candidate from a completed repo index."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from services.db import get_repo_recipe_candidate_evidence


DEFAULT_DEMO_EMAIL = "demo@hobbes.local"
DEFAULT_DEMO_PASSWORD = "password"
DEFAULT_DATABASE_URL = "postgresql://hobbes_demo:hobbes_demo@127.0.0.1:5432/hobbes_demo"
RECIPE_CANDIDATE_MODEL = os.environ.get(
    "RECIPE_CANDIDATE_MODEL",
    os.environ.get("RECIPE_PROPOSAL_MODEL", "gpt-5.4"),
)


class RecipeCandidateError(RuntimeError):
    """Raised when a recipe candidate cannot be generated."""


@dataclass(frozen=True)
class PackageCandidate:
    path: str
    cwd: str
    package_json: dict[str, Any]

    @property
    def name(self) -> str | None:
        value = self.package_json.get("name")
        return str(value) if value else None

    @property
    def scripts(self) -> dict[str, Any]:
        value = self.package_json.get("scripts")
        return value if isinstance(value, dict) else {}

    @property
    def deps(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in ("dependencies", "devDependencies"):
            value = self.package_json.get(key)
            if isinstance(value, dict):
                out.update(value)
        return out


_openai_client: OpenAI | None = None


def _client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI()
    return _openai_client


async def generate_recipe_candidate(
    repo_index_id: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = await get_repo_recipe_candidate_evidence(repo_index_id)
    if evidence is None:
        raise LookupError(f"Repo index not found: {repo_index_id}")

    repo_index = evidence["repo_index"]
    if repo_index.get("status") != "complete":
        raise RecipeCandidateError(
            f"Repo index {repo_index_id} is not complete; status={repo_index.get('status')}"
        )

    fallback = _build_deterministic_candidate(evidence)
    candidate = fallback
    candidate_source = "deterministic_index_evidence"
    model: str | None = None
    usage: dict[str, Any] = {}

    llm_disabled = (
        os.environ.get("RECIPE_CANDIDATE_DISABLE_LLM") == "true"
        or os.environ.get("RECIPE_PROPOSAL_DISABLE_LLM") == "true"
    )
    if os.environ.get("OPENAI_API_KEY") and not llm_disabled:
        try:
            llm = _call_llm(evidence, fallback, metadata=metadata or {})
            candidate = _merge_candidate(fallback, llm["candidate"])
            candidate_source = "openai_recipe_agent"
            model = llm["model"]
            usage = llm["usage"]
        except Exception as exc:  # Keep E2E unblocked if the LLM call fails.
            candidate = {
                **fallback,
                "status": "partial",
                "warnings": [
                    *list(fallback.get("warnings") or []),
                    f"LLM recipe candidate failed; using deterministic candidate: {exc}",
                ],
            }

    return {
        "repo_index_id": repo_index_id,
        "repo_url": repo_index["repo_url"],
        "commit_sha": repo_index.get("commit_sha"),
        "branch": repo_index.get("branch") or repo_index.get("default_branch"),
        "candidate_source": candidate_source,
        "model": model,
        "usage": usage,
        "candidate": candidate,
        "evidence_summary": {
            "file_count": len(evidence.get("files") or []),
            "interesting_path_count": len(evidence.get("interesting_paths") or []),
            "dir_summary_count": len(evidence.get("dir_summaries") or []),
            "code_chunk_count": len(evidence.get("code_chunks") or []),
        },
    }


def _call_llm(
    evidence: dict[str, Any],
    fallback: dict[str, Any],
    *,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    prompt = {
        "repo_index": evidence["repo_index"],
        "metadata": metadata,
        "indexed_files": evidence.get("files", [])[:350],
        "interesting_paths": evidence.get("interesting_paths", []),
        "important_file_contents": _content_by_path(evidence.get("file_lines", [])),
        "dir_summaries": evidence.get("dir_summaries", [])[:75],
        "code_chunks": evidence.get("code_chunks", [])[:35],
        "deterministic_baseline": fallback,
    }
    response = _client().chat.completions.create(
        model=RECIPE_CANDIDATE_MODEL,
        temperature=0.1,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a senior onboarding engineer generating a Hobbes Daytona "
                    "demo recipe from code-index evidence. Return only JSON. Use the "
                    "deterministic baseline as a starting point, but improve it when "
                    "the evidence clearly supports better commands, cwd values, ports, "
                    "env vars, database migration/seed steps, or demo routes. Never "
                    "invent unsupported scripts. Do not include raw source code in the "
                    "answer; cite file paths and reasons in evidence."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Produce JSON with this exact top-level shape: "
                    "{status, package_manager, config, env_template, demo, warnings, "
                    "evidence, confidence}. config must be compatible with Hobbes: "
                    "{services: {frontend/backend/...}, database?, env, demo?}. "
                    "Each service needs command, port, cwd, and optional primary/preview/"
                    "readinessTimeoutSeconds. env_template must contain placeholder "
                    "values only, never real secrets.\n\n"
                    + json.dumps(prompt, sort_keys=True)
                ),
            },
        ],
    )
    raw = response.choices[0].message.content or "{}"
    parsed = _parse_json_object(raw)
    usage = response.usage
    return {
        "candidate": parsed,
        "model": RECIPE_CANDIDATE_MODEL,
        "usage": {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0),
            "completion_tokens": getattr(usage, "completion_tokens", 0),
        },
    }


def _parse_json_object(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("Recipe candidate response was not a JSON object.")
    return parsed


def _build_deterministic_candidate(evidence: dict[str, Any]) -> dict[str, Any]:
    paths = [str(f.get("file_path")) for f in evidence.get("files", []) if f.get("file_path")]
    content = _content_by_path(evidence.get("file_lines", []))
    package_manager = _detect_package_manager(paths)
    packages = _parse_package_jsons(content)
    frontend = _pick_frontend(packages)
    backend = _pick_backend(packages, frontend)
    warnings: list[str] = []
    services: dict[str, dict[str, Any]] = {}

    if frontend:
        services["frontend"] = _service_from_package(
            frontend,
            package_manager=package_manager,
            service_kind="frontend",
        )
    else:
        warnings.append("Could not confidently identify a frontend package from indexed manifests.")

    if backend:
        services["backend"] = _service_from_package(
            backend,
            package_manager=package_manager,
            service_kind="backend",
        )

    if not services and packages:
        package = packages[0]
        services["frontend"] = _service_from_package(
            package,
            package_manager=package_manager,
            service_kind="frontend",
        )
        warnings.append("Only one runnable package was detected; treating it as the primary preview service.")

    database = _database_config(paths, package_manager, packages)
    env_template = _env_template(content)
    config: dict[str, Any] = {
        "services": services,
        "env": {
            "HOBBES_SANDBOX": "true",
            "NODE_ENV": "development",
            **env_template,
        },
        "demo": {
            "loginUrl": "http://localhost:3000/login",
            "credentials": {
                "email": DEFAULT_DEMO_EMAIL,
                "password": DEFAULT_DEMO_PASSWORD,
            },
        },
    }
    if database is not None:
        config["database"] = database

    return {
        "status": "ok" if services else "partial",
        "package_manager": package_manager,
        "config": config,
        "env_template": env_template,
        "demo": config["demo"],
        "warnings": warnings,
        "evidence": _evidence(packages, paths, database is not None),
        "confidence": 0.7 if services else 0.35,
    }


def _merge_candidate(fallback: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        return fallback
    merged = {
        **fallback,
        **{key: value for key, value in candidate.items() if value not in (None, "", [], {})},
    }
    if merged.get("package_manager") not in {"npm", "pnpm", "yarn"}:
        merged["package_manager"] = fallback.get("package_manager") or "npm"
    config = merged.get("config")
    if not isinstance(config, dict) or not isinstance(config.get("services"), dict) or not config["services"]:
        merged["config"] = fallback["config"]
        merged["status"] = "partial"
        merged["warnings"] = [
            *list(fallback.get("warnings") or []),
            "LLM candidate did not include valid services; kept deterministic config.",
        ]
    merged["env_template"] = _string_dict(merged.get("env_template"))
    if not isinstance(merged.get("demo"), dict):
        merged["demo"] = fallback.get("demo") or {}
    if not isinstance(merged.get("warnings"), list):
        merged["warnings"] = fallback.get("warnings") or []
    if not isinstance(merged.get("evidence"), list):
        merged["evidence"] = fallback.get("evidence") or []
    return merged


def _content_by_path(lines: list[dict[str, Any]]) -> dict[str, str]:
    grouped: dict[str, list[tuple[int, str]]] = {}
    for line in lines:
        path = str(line.get("file_path") or "")
        if not path:
            continue
        grouped.setdefault(path, []).append(
            (int(line.get("line_number") or 0), str(line.get("line_text") or ""))
        )
    return {
        path: "\n".join(text for _, text in sorted(values))
        for path, values in grouped.items()
    }


def _parse_package_jsons(content: dict[str, str]) -> list[PackageCandidate]:
    packages: list[PackageCandidate] = []
    for path, text in sorted(content.items()):
        if not path.endswith("package.json"):
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        cwd = path.rsplit("/", 1)[0] if "/" in path else ""
        packages.append(PackageCandidate(path=path, cwd=cwd, package_json=parsed))
    return packages


def _detect_package_manager(paths: list[str]) -> str:
    if any(path.endswith("pnpm-lock.yaml") for path in paths):
        return "pnpm"
    if any(path.endswith("yarn.lock") for path in paths):
        return "yarn"
    return "npm"


def _pick_frontend(packages: list[PackageCandidate]) -> PackageCandidate | None:
    scored: list[tuple[int, PackageCandidate]] = []
    for package in packages:
        deps = package.deps
        scripts = package.scripts
        cwd = package.cwd
        score = 0
        if "next" in deps:
            score += 5
        if "vite" in deps:
            score += 4
        if "react" in deps or "vue" in deps or "svelte" in deps:
            score += 3
        if "dev" in scripts or "start" in scripts:
            score += 1
        if cwd in {"apps/web", "web", "frontend", "client", "apps/frontend", ""}:
            score += 2
        if score >= 4:
            scored.append((score, package))
    return max(scored, key=lambda item: item[0])[1] if scored else None


def _pick_backend(
    packages: list[PackageCandidate],
    frontend: PackageCandidate | None,
) -> PackageCandidate | None:
    scored: list[tuple[int, PackageCandidate]] = []
    frontend_cwd = frontend.cwd if frontend else None
    backend_deps = {"express", "fastify", "koa", "hono", "@nestjs/core", "prisma", "@prisma/client"}
    for package in packages:
        if package.cwd == frontend_cwd:
            continue
        deps = package.deps
        scripts = package.scripts
        cwd = package.cwd
        score = 0
        if backend_deps.intersection(deps.keys()):
            score += 4
        if "dev" in scripts or "start" in scripts:
            score += 1
        if cwd in {"apps/api", "api", "backend", "server", "apps/backend"}:
            score += 3
        if score >= 4:
            scored.append((score, package))
    return max(scored, key=lambda item: item[0])[1] if scored else None


def _service_from_package(
    package: PackageCandidate,
    *,
    package_manager: str,
    service_kind: str,
) -> dict[str, Any]:
    deps = package.deps
    scripts = package.scripts
    script = _preferred_script(scripts, ("dev", "start"))
    command = _script_command(package_manager, script)
    port = _default_port(package, service_kind)
    if service_kind == "frontend":
        if "vite" in deps:
            command = f"{command} -- --host 0.0.0.0 --port {port} --strictPort"
        elif "next" in deps:
            command = f"{command} -- -H 0.0.0.0 -p {port}"
    return {
        "command": command,
        "port": port,
        "cwd": package.cwd,
        "primary": service_kind == "frontend",
        "preview": service_kind == "frontend",
        "readinessTimeoutSeconds": 240,
    }


def _preferred_script(scripts: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        if name in scripts:
            return name
    return names[0]


def _script_command(package_manager: str, script: str) -> str:
    if package_manager == "yarn":
        return f"yarn {script}"
    return f"{package_manager} run {script}"


def _default_port(package: PackageCandidate, service_kind: str) -> int:
    deps = package.deps
    if service_kind == "frontend":
        if "vite" in deps:
            return 5173
        return 3000
    return 8000


def _database_config(
    paths: list[str],
    package_manager: str,
    packages: list[PackageCandidate],
) -> dict[str, Any] | None:
    has_prisma = any(path.endswith("prisma/schema.prisma") for path in paths)
    if not has_prisma:
        return None
    database = {
        "type": "postgres",
        "envVar": "DATABASE_URL",
        "url": DEFAULT_DATABASE_URL,
        "migrate": _prisma_command(package_manager, "migrate deploy"),
    }
    seed = _pick_seed_script(package_manager, packages)
    if seed:
        database["seed"] = seed
    return database


def _prisma_command(package_manager: str, args: str) -> str:
    if package_manager == "pnpm":
        return f"pnpm exec prisma {args}"
    if package_manager == "yarn":
        return f"yarn prisma {args}"
    return f"npx prisma {args}"


def _pick_seed_script(package_manager: str, packages: list[PackageCandidate]) -> str | None:
    for script in ("seed:demo", "seed"):
        for package in packages:
            if script in package.scripts:
                return _script_command(package_manager, script)
    return None


def _env_template(content: dict[str, str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for path, text in content.items():
        name = path.rsplit("/", 1)[-1]
        if name not in {
            ".env.example",
            ".env.sample",
            ".env.template",
            ".env.dist",
            "env.example",
            "env.sample",
            "env.template",
            "env.dist",
        }:
            continue
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not re.match(r"^[A-Z][A-Z0-9_]*$", key):
                continue
            env[key] = _placeholder_value(value.strip().strip("'\""))
    return env


def _placeholder_value(value: str) -> str:
    if not value or re.search(r"(secret|token|key|password|private)", value, re.IGNORECASE):
        return ""
    if len(value) > 120:
        return ""
    return value


def _string_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): "" if val is None else str(val) for key, val in value.items()}


def _evidence(
    packages: list[PackageCandidate],
    paths: list[str],
    has_database: bool,
) -> list[dict[str, str]]:
    evidence = [
        {
            "path": package.path,
            "reason": "package manifest used for scripts, dependencies, and cwd inference",
        }
        for package in packages[:8]
    ]
    for marker in (
        "pnpm-lock.yaml",
        "package-lock.json",
        "yarn.lock",
        "prisma/schema.prisma",
        "docker-compose.yml",
        "docker-compose.yaml",
    ):
        if any(path.endswith(marker) for path in paths):
            evidence.append({"path": marker, "reason": "runtime setup signal"})
    if has_database:
        evidence.append({"path": "prisma/schema.prisma", "reason": "Postgres database inferred"})
    return evidence[:12]
