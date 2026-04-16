"""
test_ask_question.py — smoke tests for the /askQuestion endpoint.

Indexes the target repo, then runs 10 questions across Explorer / Explainer /
Tracer categories, checking both routing (last_agent == expected) and whether
the response is grounded (cites real file paths / identifiers).

Usage:
    python3 scripts/test_ask_question.py
    python3 scripts/test_ask_question.py --base http://localhost:8000
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

REPO_URL = "https://github.com/jerrym1299/codebase-onboading-agent"

# Each test: (category, expected_agent, question, grounding_tokens)
# A response passes grounding if ANY of its tokens appears verbatim.
TESTS = [
    ("explorer", "Explorer",
     "where is main.py",
     ["main.py"]),
    ("explorer", "Explorer",
     "find the chunk_file_list function",
     ["chunk_and_embed.py", "chunk_file_list"]),
    ("explorer", "Explorer",
     "where is the dockerfile",
     ["dockerfile", "Dockerfile"]),
    ("explorer", "Explorer",
     "find the SEARCH_SQL constant",
     ["SEARCH_SQL", "main.py"]),

    ("explainer", "Explainer",
     "explain how semantic search works in this codebase",
     ["embedding", "pgvector", "search_endpoint", "embed_query"]),
    ("explainer", "Explainer",
     "how is the chunking pipeline organized",
     ["chunk_file_list", "extract_chunks_from_file", "tree-sitter", "tree_sitter"]),
    ("explainer", "Explainer",
     "explain how the router_agent dispatches questions",
     ["router_agent", "handoff", "Explorer", "Explainer", "Tracer"]),

    ("tracer", "Tracer",
     "trace what happens when the chunks endpoint is called",
     ["ensure_repo_dir", "collect_file_paths", "chunk_file_list", "store_chunks"]),
    ("tracer", "Tracer",
     "what does ensure_repo_dir call",
     ["clone_repo", "os.path.isdir", "isdir"]),
    ("tracer", "Tracer",
     "trace the flow from askQuestion endpoint to the Explainer agent",
     ["router_agent", "Runner.run", "handoff", "Explainer"]),
]


GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"


def _http_get(url: str, timeout: int = 180) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _encode_query(q: str) -> str:
    return q.replace(" ", "+")


def index_repo(base: str, repo_url: str) -> None:
    print(f"{DIM}Indexing {repo_url} …{RESET}")
    t0 = time.time()
    encoded_url = urllib.parse.quote(repo_url, safe="")
    data = _http_get(f"{base}/chunks?repo_url={encoded_url}&preview=0", timeout=600)
    dt = time.time() - t0
    if "error" in data:
        sys.exit(f"Index failed: {data['error']}")
    print(f"  files={data['file_count']} chunks={data['chunk_count']} "
          f"stored={data['stored']} ({dt:.1f}s)\n")


def run_test(base: str, idx: int, category: str, expected_agent: str,
             question: str, grounding: list[str]) -> tuple[bool, bool, str, str]:
    encoded_url = urllib.parse.quote(REPO_URL, safe="")
    url = f"{base}/askQuestion?repo_url={encoded_url}&query={_encode_query(question)}"
    try:
        data = _http_get(url)
    except Exception as exc:
        return False, False, "ERROR", f"HTTP error: {exc}"

    actual_agent = data.get("last_agent", "?")
    response = data.get("response", "") or ""
    route_ok = actual_agent == expected_agent
    grounded_ok = any(tok.lower() in response.lower() for tok in grounding)
    return route_ok, grounded_ok, actual_agent, response


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--skip-index", action="store_true",
                    help="Assume the repo is already indexed.")
    ap.add_argument("--output", default="output.txt",
                    help="File to write full Q/A transcript to (default output.txt).")
    args = ap.parse_args()

    if not args.skip_index:
        index_repo(args.base, REPO_URL)

    results: list[tuple[int, str, bool, bool, str]] = []
    transcript: list[str] = [
        f"askQuestion test run — repo={REPO_URL} base={args.base}",
        "=" * 72,
        "",
    ]

    for i, (category, expected, question, grounding) in enumerate(TESTS, start=1):
        print(f"[{i:>2}/{len(TESTS)}] {category:<9} → expect {expected:<9} "
              f"| {question}")
        route_ok, grounded_ok, actual, response = run_test(
            args.base, i, category, expected, question, grounding
        )
        tag_route = f"{GREEN}ROUTE{RESET}" if route_ok else f"{RED}ROUTE{RESET}"
        tag_ground = f"{GREEN}GROUND{RESET}" if grounded_ok else f"{RED}GROUND{RESET}"
        print(f"       {tag_route}={actual}  {tag_ground}")
        if not (route_ok and grounded_ok):
            preview = response[:300].replace("\n", " ")
            print(f"       {YELLOW}preview:{RESET} {preview}…")
        print()
        results.append((i, category, route_ok, grounded_ok, actual))

        transcript.extend([
            f"[{i:02d}] {category.upper()}  (expected: {expected}, actual: {actual})",
            f"     route={'PASS' if route_ok else 'FAIL'}  "
            f"grounded={'PASS' if grounded_ok else 'FAIL'}",
            f"Q: {question}",
            "A:",
            response.rstrip() or "(empty response)",
            "-" * 72,
            "",
        ])

    # Summary
    route_pass = sum(1 for _, _, r, _, _ in results if r)
    ground_pass = sum(1 for _, _, _, g, _ in results if g)
    both_pass = sum(1 for _, _, r, g, _ in results if r and g)
    total = len(results)

    summary = [
        "Summary",
        f"  Routing:  {route_pass}/{total}",
        f"  Grounded: {ground_pass}/{total}",
        f"  Both:     {both_pass}/{total}",
    ]
    transcript.extend(summary)

    with open(args.output, "w", encoding="utf-8") as f:
        f.write("\n".join(transcript) + "\n")

    print("=" * 60)
    for line in summary[1:]:
        print(line.strip())
    print(f"Wrote {args.output}")
    sys.exit(0 if both_pass == total else 1)


if __name__ == "__main__":
    main()
