"""Unit tests for services/dependency_graph.py.

Hand-built BoundaryReport fixtures exercise:
  - shared_infra dedupe across repos
  - HTTP matching by resolved port
  - HTTP matching by env-name heuristic
  - unresolved consumed.http -> ambiguity
  - hard_runtime promotion via startup-plan migration step
  - dev_proxy keeps source soft
  - cycle break via demoting a hard_runtime edge

Usage:
    python3 scripts/test_dependency_graph.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.boundary_extractor import (
    BoundaryReport, ConsumedDb, ConsumedHttp, DevProxy, ExposedHttp,
    RequiredService,
)
from services.dependency_graph import build_graph


REPO_A = "https://github.com/acme/api"
REPO_B = "https://github.com/acme/web"
REPO_C = "https://github.com/acme/worker"


def _plan_with_migration() -> dict:
    return {"packages": [{"steps": [{"command": "alembic upgrade head", "title": "migrate"}]}]}


def assert_eq(a, b, label):
    if a != b:
        print(f"FAIL [{label}]: expected {b!r}, got {a!r}")
        sys.exit(1)
    print(f"  ok: {label}")


def assert_true(cond, label):
    if not cond:
        print(f"FAIL [{label}]")
        sys.exit(1)
    print(f"  ok: {label}")


def test_shared_infra_dedupe():
    print("test_shared_infra_dedupe")
    a = BoundaryReport(repo_url=REPO_A, required_services=[RequiredService(kind="postgres", via="DATABASE_URL")])
    b = BoundaryReport(repo_url=REPO_B, required_services=[RequiredService(kind="postgres", via="DATABASE_URL")])
    graph, _, _ = build_graph([(REPO_A, "/tmp/a", a, None), (REPO_B, "/tmp/b", b, None)])
    infra_nodes = [n for n in graph.nodes if n.kind == "infra"]
    assert_eq(len(infra_nodes), 1, "one shared postgres node")
    infra_id = infra_nodes[0].id
    edges = [e for e in graph.edges if e.target == infra_id]
    assert_eq(sorted(e.source for e in edges), [REPO_A, REPO_B], "both repos consume it")
    assert_true(all(e.edge_type == "shared_infra" for e in edges), "edges are shared_infra")


def test_http_match_by_port():
    print("test_http_match_by_port")
    api = BoundaryReport(repo_url=REPO_A, exposed=[ExposedHttp(method="GET", path="/users")])
    web = BoundaryReport(repo_url=REPO_B, consumed=[
        ConsumedHttp(target_env="API_URL", resolved="http://localhost:4001", path="/users"),
    ])
    graph, _, ambig = build_graph([(REPO_A, "/tmp/a", api, None), (REPO_B, "/tmp/b", web, None)])
    edges = [e for e in graph.edges if e.source == REPO_B and e.target == REPO_A]
    assert_eq(len(edges), 1, "one HTTP edge web -> api")
    assert_eq(edges[0].edge_type, "soft_runtime", "default soft_runtime")
    assert_eq(ambig, [], "no ambiguities")


def test_http_match_by_env_name():
    print("test_http_match_by_env_name")
    api = BoundaryReport(repo_url=REPO_A, exposed=[ExposedHttp(method="GET", path="/")])
    web = BoundaryReport(repo_url=REPO_B, consumed=[
        ConsumedHttp(target_env="API_URL"),
    ])
    graph, _, _ = build_graph([(REPO_A, "/tmp/a", api, None), (REPO_B, "/tmp/b", web, None)])
    edges = [e for e in graph.edges if e.source == REPO_B and e.target == REPO_A]
    assert_eq(len(edges), 1, "env-name heuristic matched")
    assert_eq(edges[0].confidence, 0.4, "env-name confidence is 0.4")


def test_unresolved_http_becomes_ambiguity():
    print("test_unresolved_http_becomes_ambiguity")
    web = BoundaryReport(repo_url=REPO_B, consumed=[ConsumedHttp(target_env="THIRD_PARTY_API")])
    _, _, ambig = build_graph([(REPO_B, "/tmp/b", web, None)])
    assert_eq(len(ambig), 1, "one ambiguity")
    assert_true("THIRD_PARTY_API" in ambig[0].reason, "names the env var")


def test_promotion_to_hard_via_migration_step():
    print("test_promotion_to_hard_via_migration_step")
    api = BoundaryReport(repo_url=REPO_A, exposed=[ExposedHttp(method="GET", path="/")])
    worker = BoundaryReport(repo_url=REPO_C, consumed=[
        ConsumedHttp(target_env="API_URL", resolved="http://localhost:4001"),
    ])
    graph, _, _ = build_graph([
        (REPO_A, "/tmp/a", api, None),
        (REPO_C, "/tmp/c", worker, _plan_with_migration()),
    ])
    edge = next(e for e in graph.edges if e.source == REPO_C and e.target == REPO_A)
    assert_eq(edge.edge_type, "hard_runtime", "promoted to hard via migration step")
    api_first = graph.topo_order.index([REPO_A]) if [REPO_A] in graph.topo_order else None
    assert_true(api_first is not None and api_first == 0, "REPO_A starts before REPO_C")


def test_dev_proxy_keeps_source_soft():
    print("test_dev_proxy_keeps_source_soft")
    api = BoundaryReport(repo_url=REPO_A, exposed=[ExposedHttp(method="GET", path="/")])
    web = BoundaryReport(
        repo_url=REPO_B,
        consumed=[ConsumedHttp(target_env="API_URL", resolved="http://localhost:4001")],
        dev_proxy=[DevProxy(from_path="/api", to_env="API_URL", config_file="vite.config.ts")],
    )
    web_plan = _plan_with_migration()
    graph, _, _ = build_graph([
        (REPO_A, "/tmp/a", api, None),
        (REPO_B, "/tmp/b", web, web_plan),
    ])
    edge = next(e for e in graph.edges if e.source == REPO_B and e.target == REPO_A)
    assert_eq(edge.edge_type, "soft_runtime", "dev_proxy source stays soft despite migration step")


def test_cycle_break_via_demotion():
    print("test_cycle_break_via_demotion")
    a = BoundaryReport(
        repo_url=REPO_A,
        exposed=[ExposedHttp(method="GET", path="/")],
        consumed=[ConsumedHttp(target_env="WEB_URL", resolved="http://localhost:3000")],
    )
    b = BoundaryReport(
        repo_url=REPO_B,
        exposed=[ExposedHttp(method="GET", path="/")],
        consumed=[ConsumedHttp(target_env="API_URL", resolved="http://localhost:4001")],
    )
    a_plan = _plan_with_migration()
    b_plan = _plan_with_migration()
    graph, _, _ = build_graph([
        (REPO_A, "/tmp/a", a, a_plan),
        (REPO_B, "/tmp/b", b, b_plan),
    ])
    assert_true(len(graph.cycle_breaks) >= 1, "cycle was detected and broken")
    flat = [n for group in graph.topo_order for n in group]
    assert_true(REPO_A in flat and REPO_B in flat, "both repos still appear in topo order")


def test_db_consumed_creates_infra_node():
    print("test_db_consumed_creates_infra_node")
    a = BoundaryReport(
        repo_url=REPO_A,
        consumed=[ConsumedDb(engine="mysql", target_env="DATABASE_URL")],
    )
    graph, _, _ = build_graph([(REPO_A, "/tmp/a", a, None)])
    infras = [n for n in graph.nodes if n.kind == "infra"]
    assert_eq(len(infras), 1, "one mysql infra node")
    assert_eq(infras[0].infra_kind, "mysql", "kind is mysql")


def main():
    test_shared_infra_dedupe()
    test_http_match_by_port()
    test_http_match_by_env_name()
    test_unresolved_http_becomes_ambiguity()
    test_promotion_to_hard_via_migration_step()
    test_dev_proxy_keeps_source_soft()
    test_cycle_break_via_demotion()
    test_db_consumed_creates_infra_node()
    print("\nALL PASSED")


if __name__ == "__main__":
    main()
