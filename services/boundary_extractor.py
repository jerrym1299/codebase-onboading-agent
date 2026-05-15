"""Per-repo wire-boundary extraction.

The agent reads a freshly cloned repo and emits a `BoundaryReport` describing
what HTTP routes the repo exposes, what HTTP/DB endpoints it consumes, what
dev-server proxies it configures, and what infra services it requires. The
report is the input to the cross-repo dependency matcher in
`services/dependency_graph.py`.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field


class ExposedHttp(BaseModel):
    kind: Literal["http"] = "http"
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "ANY"]
    path: str
    handler: str | None = None


class ConsumedHttp(BaseModel):
    kind: Literal["http"] = "http"
    target_env: str | None = None
    resolved: str | None = None
    resolved_from: str | None = None
    path: str | None = None


class ConsumedDb(BaseModel):
    kind: Literal["db"] = "db"
    engine: Literal["postgres", "mysql", "sqlite", "mongodb", "redis", "other"]
    target_env: str | None = None
    resolved: str | None = None
    resolved_from: str | None = None


class DevProxy(BaseModel):
    from_path: str
    to_env: str | None = None
    to_resolved: str | None = None
    config_file: str


class RequiredService(BaseModel):
    kind: Literal["postgres", "mysql", "redis", "elasticsearch", "other"]
    via: str | None = None


class Ambiguity(BaseModel):
    field: str
    reason: str


class BoundaryReport(BaseModel):
    repo_url: str
    exposed: list[ExposedHttp] = Field(default_factory=list)
    consumed: list[ConsumedHttp | ConsumedDb] = Field(default_factory=list)
    dev_proxy: list[DevProxy] = Field(default_factory=list)
    required_services: list[RequiredService] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)


_DEVELOPER_PROMPT_TEMPLATE = """Local codebase path: {repo_dir}
Indexed repo_url: {repo_url}

Per-repo startup plan (already generated; runtime/package manager/env vars are
known from this — focus on WIRE BOUNDARIES, not runtime details):
{startup_plan_json}
"""


def build_developer_prompt(
    repo_dir: str, repo_url: str, startup_plan: dict | None
) -> str:
    plan_json = json.dumps(startup_plan, indent=2) if startup_plan else "(none)"
    return _DEVELOPER_PROMPT_TEMPLATE.format(
        repo_dir=repo_dir,
        repo_url=repo_url,
        startup_plan_json=plan_json,
    )
