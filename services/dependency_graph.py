"""Deterministic cross-repo dependency matcher.

Inputs: per-repo BoundaryReport + startup plan + repo_dir.
Output: typed DependencyGraph (RepoNodes + InfraNodes + classified Edges +
topological order in parallel groups + cycle-break records), plus the raw
orchestration parse findings and any unresolved ambiguities.

Pure-code, no LLM. Called from build_graph_activity.
"""

from __future__ import annotations

import re
from collections import defaultdict
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from services.boundary_extractor import BoundaryReport


class RepoNode(BaseModel):
    kind: Literal["repo"] = "repo"
    id: str
    name: str


class InfraNode(BaseModel):
    kind: Literal["infra"] = "infra"
    id: str
    infra_kind: Literal["postgres", "mysql", "redis", "elasticsearch", "other"]
    target_env: str | None = None


class Edge(BaseModel):
    source: str
    target: str
    edge_type: Literal["hard_runtime", "soft_runtime", "shared_infra"]
    confidence: float
    evidence: list[str]
    match_reason: str


class CycleBreak(BaseModel):
    cycle: list[str]
    broken_edge: tuple[str, str]
    reason: str


class DependencyGraph(BaseModel):
    nodes: list[RepoNode | InfraNode]
    edges: list[Edge]
    topo_order: list[list[str]]
    cycle_breaks: list[CycleBreak]


class OrchestrationFinding(BaseModel):
    repo_url: str
    file: str
    parsed_services: list[str] = []
    parsed_dependencies: list[tuple[str, str]] = []


class GraphAmbiguity(BaseModel):
    repo_url: str | None = None
    field: str
    reason: str


_INFRA_ALIASES = {
    "postgres": "postgres", "postgresql": "postgres", "pg": "postgres",
    "mysql": "mysql", "mariadb": "mysql",
    "redis": "redis",
    "elasticsearch": "elasticsearch", "elastic": "elasticsearch",
    "sqlite": "other", "mongodb": "other", "mongo": "other",
    "other": "other",
}


def _repo_short_name(repo_url: str) -> str:
    return repo_url.rstrip("/").split("/")[-1].removesuffix(".git")


def _normalize_url(value: str | None) -> tuple[str | None, int | None, str]:
    """Return (host, port, path_prefix). Resolves common localhost variants."""
    if not value:
        return (None, None, "")
    raw = value.strip()
    if not raw:
        return (None, None, "")
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parsed = urlparse(raw)
    except ValueError:
        return (None, None, "")
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"}:
        host = "localhost"
    return (host or None, parsed.port, (parsed.path or "").rstrip("/"))


# ---- orchestration parse ---------------------------------------------------

_COMPOSE_FILES = (
    "docker-compose.yml", "docker-compose.yaml",
    "compose.yml", "compose.yaml",
)


def parse_orchestration(repo_url: str, repo_dir: str) -> list[OrchestrationFinding]:
    """Parse docker-compose / Procfile entries for a repo."""
    out: list[OrchestrationFinding] = []
    root = Path(repo_dir)
    if not root.is_dir():
        return out

    for name in _COMPOSE_FILES:
        path = root / name
        if path.is_file():
            out.append(_parse_compose(repo_url, path))

    procfile = root / "Procfile"
    if procfile.is_file():
        out.append(_parse_procfile(repo_url, procfile))

    return out


def _parse_compose(repo_url: str, path: Path) -> OrchestrationFinding:
    services: list[str] = []
    deps: list[tuple[str, str]] = []
    if yaml is None:
        return OrchestrationFinding(
            repo_url=repo_url, file=str(path),
            parsed_services=services, parsed_dependencies=deps,
        )
    try:
        data = yaml.safe_load(path.read_text(errors="replace")) or {}
    except yaml.YAMLError:
        return OrchestrationFinding(
            repo_url=repo_url, file=str(path),
            parsed_services=services, parsed_dependencies=deps,
        )
    svc_block = data.get("services") if isinstance(data, dict) else None
    if not isinstance(svc_block, dict):
        return OrchestrationFinding(
            repo_url=repo_url, file=str(path),
            parsed_services=services, parsed_dependencies=deps,
        )
    for svc_name, svc_def in svc_block.items():
        services.append(svc_name)
        if not isinstance(svc_def, dict):
            continue
        depends = svc_def.get("depends_on")
        if isinstance(depends, list):
            for dep in depends:
                if isinstance(dep, str):
                    deps.append((svc_name, dep))
        elif isinstance(depends, dict):
            for dep in depends:
                deps.append((svc_name, dep))
    return OrchestrationFinding(
        repo_url=repo_url, file=str(path),
        parsed_services=services, parsed_dependencies=deps,
    )


def _parse_procfile(repo_url: str, path: Path) -> OrchestrationFinding:
    services: list[str] = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([\w-]+):", line)
        if m:
            services.append(m.group(1))
    return OrchestrationFinding(
        repo_url=repo_url, file=str(path),
        parsed_services=services, parsed_dependencies=[],
    )


# ---- graph construction ----------------------------------------------------


def _infra_id(infra_kind: str, target_env: str | None) -> str:
    return f"{infra_kind}:{target_env or '*'}"


def _build_infra_nodes(
    repos: list[tuple[str, str, BoundaryReport, dict | None]],
    findings: list[OrchestrationFinding],
) -> tuple[list[InfraNode], dict[str, set[str]]]:
    """Returns (nodes, infra_id -> set of repo_urls that consume it)."""
    nodes: dict[str, InfraNode] = {}
    consumers: dict[str, set[str]] = defaultdict(set)

    for repo_url, _repo_dir, report, _plan in repos:
        for req in report.required_services:
            kind = _INFRA_ALIASES.get(req.kind, req.kind)
            iid = _infra_id(kind, req.via)
            if iid not in nodes:
                nodes[iid] = InfraNode(id=iid, infra_kind=kind, target_env=req.via)
            consumers[iid].add(repo_url)
        for c in report.consumed:
            if c.kind != "db":
                continue
            kind = _INFRA_ALIASES.get(c.engine, c.engine)
            iid = _infra_id(kind, c.target_env)
            if iid not in nodes:
                nodes[iid] = InfraNode(id=iid, infra_kind=kind, target_env=c.target_env)
            consumers[iid].add(repo_url)

    for f in findings:
        for svc in f.parsed_services:
            kind = _INFRA_ALIASES.get(svc.lower())
            if kind is None:
                continue
            iid = _infra_id(kind, None)
            if iid not in nodes:
                nodes[iid] = InfraNode(id=iid, infra_kind=kind, target_env=None)
            consumers[iid].add(f.repo_url)

    return list(nodes.values()), consumers


def _match_http_edges(
    repos: list[tuple[str, str, BoundaryReport, dict | None]],
) -> tuple[list[Edge], list[GraphAmbiguity]]:
    edges: list[Edge] = []
    ambiguities: list[GraphAmbiguity] = []

    exposed_index: list[tuple[str, int, str]] = []
    for repo_url, _repo_dir, report, _plan in repos:
        for idx, ex in enumerate(report.exposed):
            exposed_index.append((repo_url, idx, (ex.path or "").rstrip("/")))

    for src_url, _src_dir, report, _plan in repos:
        for cidx, c in enumerate(report.consumed):
            if c.kind != "http":
                continue
            host, port, path = _normalize_url(c.resolved)
            matched = False

            if port is not None:
                for tgt_url, eidx, epath in exposed_index:
                    if tgt_url == src_url:
                        continue
                    if not path or epath == "" or epath.startswith(path) or path.startswith(epath):
                        edges.append(Edge(
                            source=src_url, target=tgt_url,
                            edge_type="soft_runtime",
                            confidence=0.6,
                            evidence=[f"{src_url}.consumed[{cidx}]",
                                      f"{tgt_url}.exposed[{eidx}]"],
                            match_reason=f"port+path: {host}:{port}{path}",
                        ))
                        matched = True
                        break

            if not matched and c.target_env:
                env_name = c.target_env.lower().replace("-", "_")
                for tgt_url, _tgt_dir, _r, _p in repos:
                    if tgt_url == src_url:
                        continue
                    tgt_short = _repo_short_name(tgt_url).lower().replace("-", "_")
                    if tgt_short and tgt_short in env_name:
                        edges.append(Edge(
                            source=src_url, target=tgt_url,
                            edge_type="soft_runtime",
                            confidence=0.4,
                            evidence=[f"{src_url}.consumed[{cidx}]"],
                            match_reason=f"env_name_heuristic: {c.target_env}",
                        ))
                        matched = True
                        break

            if not matched and c.target_env and not c.resolved:
                ambiguities.append(GraphAmbiguity(
                    repo_url=src_url,
                    field=f"consumed[{cidx}]",
                    reason=f"target_env={c.target_env!r} could not be resolved to a known repo",
                ))

    seen: set[tuple[str, str, str]] = set()
    deduped: list[Edge] = []
    for e in edges:
        key = (e.source, e.target, e.match_reason)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    return deduped, ambiguities


def _classify_hard_soft(
    edges: list[Edge],
    repos: list[tuple[str, str, BoundaryReport, dict | None]],
) -> list[Edge]:
    proxy_sources = {url for url, _, report, _ in repos if report.dev_proxy}
    plan_by_repo = {url: plan for url, _, _, plan in repos}

    out: list[Edge] = []
    for e in edges:
        if e.edge_type == "shared_infra":
            out.append(e)
            continue
        if e.source in proxy_sources:
            out.append(e)
            continue
        plan = plan_by_repo.get(e.source) or {}
        promoted = False
        for pkg in plan.get("packages", []) or []:
            for step in pkg.get("steps", []) or []:
                blob = ((step.get("command") or "") + " " + (step.get("title") or "")).lower()
                if any(k in blob for k in ("migrate", "wait-for", "healthcheck", "seed")):
                    promoted = True
                    break
            if promoted:
                break
        if promoted:
            out.append(e.model_copy(update={
                "edge_type": "hard_runtime",
                "confidence": min(1.0, e.confidence + 0.2),
            }))
        else:
            out.append(e)
    return out


def _topo_sort(
    nodes: list[RepoNode | InfraNode], edges: list[Edge],
) -> tuple[list[list[str]], list[CycleBreak]]:
    """Topological sort over hard edges only via graphlib.TopologicalSorter.
    Soft edges are preferences, ignored here.

    Edge semantics: source depends on target (target must come up first), so
    we register `target` as a predecessor of `source`."""
    cycle_breaks: list[CycleBreak] = []
    active = [e for e in edges if e.edge_type in {"hard_runtime", "shared_infra"}]
    node_ids = {n.id for n in nodes}

    while True:
        ts: TopologicalSorter[str] = TopologicalSorter()
        for nid in node_ids:
            ts.add(nid)
        for e in active:
            if e.source in node_ids and e.target in node_ids:
                ts.add(e.source, e.target)

        try:
            ts.prepare()
        except CycleError as ce:
            cycle_path = list(ce.args[1])
            cycle_set = set(cycle_path)
            demotable = next(
                (e for e in active
                 if e.source in cycle_set and e.target in cycle_set
                 and e.edge_type == "hard_runtime"),
                None,
            )
            if demotable is None:
                cycle_breaks.append(CycleBreak(
                    cycle=cycle_path,
                    broken_edge=("", ""),
                    reason="unresolvable_cycle_no_demotable_edge",
                ))
                return [cycle_path], cycle_breaks
            active.remove(demotable)
            cycle_breaks.append(CycleBreak(
                cycle=cycle_path,
                broken_edge=(demotable.source, demotable.target),
                reason="demoted_hard_runtime_to_break_cycle",
            ))
            continue

        groups: list[list[str]] = []
        while ts.is_active():
            ready = sorted(ts.get_ready())
            if not ready:
                break
            groups.append(ready)
            ts.done(*ready)
        return groups, cycle_breaks


def build_graph(
    repos: list[tuple[str, str, BoundaryReport, dict | None]],
) -> tuple[DependencyGraph, list[OrchestrationFinding], list[GraphAmbiguity]]:
    findings: list[OrchestrationFinding] = []
    for repo_url, repo_dir, _report, _plan in repos:
        findings.extend(parse_orchestration(repo_url, repo_dir))

    repo_nodes = [RepoNode(id=u, name=_repo_short_name(u)) for u, _, _, _ in repos]
    infra_nodes, infra_consumers = _build_infra_nodes(repos, findings)
    nodes: list[RepoNode | InfraNode] = list(repo_nodes) + list(infra_nodes)

    infra_edges: list[Edge] = []
    for infra in infra_nodes:
        for consumer in sorted(infra_consumers.get(infra.id, [])):
            infra_edges.append(Edge(
                source=consumer,
                target=infra.id,
                edge_type="shared_infra",
                confidence=0.9,
                evidence=[f"{consumer}.required_services|consumed.db"],
                match_reason=f"infra:{infra.infra_kind}",
            ))

    http_edges, ambiguities = _match_http_edges(repos)
    classified = _classify_hard_soft(infra_edges + http_edges, repos)
    topo_order, cycle_breaks = _topo_sort(nodes, classified)

    graph = DependencyGraph(
        nodes=nodes,
        edges=classified,
        topo_order=topo_order,
        cycle_breaks=cycle_breaks,
    )
    return graph, findings, ambiguities
