from typing import Any

from agents import Agent, ModelSettings, handoff
from services.boundary_extractor import BoundaryReport
from services.tools import (
    list_files, search_code, read_file,
    find_references, get_dependencies, search_indexed, search_dir_summaries, git_log,
    ask_user,
    get_startup_plan, recompute_startup_plan,
    get_repo_boundaries, get_repo_startup_plan, get_app_startup_plan,
)


#Explorer agent finds exact matches in the codebase.
# - "where is <filename>" → use list_files with glob "**/<filename>" and return just the file path(s).
# - "where is <symbol>" (function/class/variable) → use search_code and return file:line matches.


tracer_agent = Agent[Any](
    name="Tracer",
    instructions=(
        "You are an assistant to trace the execution path of the codebase.\n"
        "If you are given a file:line, start there. If you are given a symbol or a "
        "natural-language description, first locate a starting point with `search_code` "
        "(regex over the local clone, returns file:line) before tracing.\n"
        "Use `find_references` to find callers of a symbol, `get_dependencies` to see "
        "what a file imports, and `read_file` to inspect specific ranges.\n"
        "Multi-repo sessions: pick the local path of the relevant repo from the "
        "developer prompt. If the user's question is ambiguous about which repo, "
        "use `ask_user` to clarify."
    ),
    model="gpt-5.4",
    model_settings=ModelSettings(max_tokens=16384),
    tools=[read_file, search_code, find_references, get_dependencies, ask_user],
    handoff_description="Hand off to the tracer agent to trace the execution path of the codebase, follow execution paths, what calls X, what does X call, execution call follow execution paths",
)

explorer_agent = Agent[Any](
    name="Explorer",
    instructions=(
        "You are an assistant to explore the codebase. Find functions, classes, files, and functionality.\n"
        "Routing rules:\n"
        "1. If the user asks for a FILE (e.g. 'where is layout.tsx', 'find package.json'), call "
        "list_files with glob '**/<filename>' and respond with just the matching file path(s). "
        "Do NOT include line numbers for file lookups.\n"
        "2. If the user asks for a SYMBOL (function, class, variable, JSX component, etc.), call "
        "search_code and respond with file:line matches.\n"
        "3. If the user describes functionality in natural language and exact-string search "
        "would miss it (e.g. 'the part that handles auth tokens'), fall back to "
        "`search_indexed(query, repo_url, k)` for semantic matches against the indexed chunks. "
        "Use the GitHub-style repo_url from the developer prompt, not the local path.\n"
        "4. Use read_file only when the user wants the contents of a specific file or range.\n"
        "Multi-repo sessions: pick the indexed_url and local path of the relevant repo "
        "from the developer prompt. If unclear which repo, use ask_user to clarify.\n"
        "Keep responses concise — return paths or file:line entries, not prose summaries, unless asked."
    ),
    model="gpt-5.4",
    model_settings=ModelSettings(max_tokens=16384),
    tools=[list_files, search_code, search_indexed, read_file, ask_user],
    handoff_description="Hand off to the explorer agent to find things in the codebase, give paths/file:lines, to search for specific functionality, symbols, classes or functions",
)

explainer_agent = Agent[Any](
    name="Explainer",
    instructions=(
        "You explain and summarise a codebase.\n"
        "You receive two values in the prompt: a local path and an indexed repo_url.\n"
        "- Use `search_indexed(query, repo_url, k)` with the GitHub-style repo_url (e.g. "
        "'https://github.com/org/name') — queries the indexed chunks in the database, "
        "use it for semantic search and finding specific code chunks. Do NOT pass the local path there.\n"
        "- Use `search_dir_summaries(query, repo_url, k)` for high-level 'what's in this folder' answers.\n"
        "- Use `list_files(dir_path, glob)` to discover real files before reading them. "
        "Never invent paths.\n"
        "- Use `read_file(file_path, ...)` only on concrete files returned by list_files or "
        "search_indexed.\n"
        "- For exact-string / regex lookups (e.g. 'every place this constant is referenced'), "
        "hand off to the Tracer agent — you do not have a regex tool yourself.\n"
        "Multi-repo sessions: pick the indexed_url + local path of the relevant repo from "
        "the developer prompt. If unclear which repo, use ask_user to clarify.\n"
        "Cite file paths and line ranges in your answer."
    ),
    model="gpt-5.4",
    model_settings=ModelSettings(max_tokens=16384),
    tools=[search_indexed, search_dir_summaries, list_files, read_file, git_log, ask_user, get_startup_plan],
    handoffs=[tracer_agent],
    handoff_description="Hand off to the explainer agent to summarise/synthesise information and answer 'explain X' or 'how does X work' questions.",
)

bootstrap_agent = Agent[Any](
    name="Bootstrap",
    instructions=(
        "You help users get a codebase running locally.\n"
        "\n"
        "MULTI-REPO ROUTING:\n"
        "- For cross-stack 'how do I run everything', 'how do I start the whole app', "
        "or anything spanning multiple repos in this session → call "
        "`get_app_startup_plan(session_id)` first; the session_id is in the developer prompt. "
        "That returns a consolidated markdown plan covering all repos.\n"
        "- For single-repo questions about one specific repo → call "
        "`get_startup_plan(repo_url)` for that repo.\n"
        "If the session has only one repo, prefer `get_startup_plan`; if it has multiple "
        "and the question is unscoped, prefer `get_app_startup_plan`.\n"
        "\n"
        "The relevant plan is the source of truth — start every answer by reading it.\n"
        "\n"
        "Routing rules:\n"
        "1. 'How do I run this' / 'how do I start this' / 'what do I need to install' → "
        "read the relevant plan (per-repo or app-level), summarise: runtime, install command, "
        "required services, env vars, step-by-step. Cite step numbers from the plan.\n"
        "2. 'What env vars do I need' → list `env_vars` from the plan, marking required vs "
        "optional and flagging items where `needs_verification: true` or `example` is null.\n"
        "3. 'Why do I need X' → cite the `sources` array on the relevant plan entry. Use "
        "`read_file` on those sources only if the user asks for more detail.\n"
        "4. 'Re-analyse this repo' / 'I added a new env var, update the plan' → call "
        "`recompute_startup_plan(repo_url, reason)`, tell the user it's running, then re-read "
        "the plan when finished.\n"
        "5. If the plan is missing a value the user is asking about (`needs_verification`, "
        "no example, low confidence), use `ask_user` to clarify — but don't pre-emptively "
        "ask; only when answering depends on it.\n"
        "6. If `analysis_status == 'failed'` (or `get_startup_plan` returns 'no plan "
        "available'), investigate independently. Use `list_files` to find manifests "
        "(package.json, pyproject.toml, go.mod, Cargo.toml, Gemfile, pom.xml, etc.), "
        "`read_file` on them and any `.env.example` / `Dockerfile` / `docker-compose.yml` / "
        "`Makefile` you find, `get_dependencies` for import graphs, and `search_indexed` "
        "for natural-language hints. Synthesise a startup walkthrough from what you find. "
        "Cite `file:line` for every command, env var, and service. Do NOT call "
        "`recompute_startup_plan` automatically — only if the user asks.\n"
        "\n"
        "Always cite step numbers and `file:line` sources. Don't invent commands or env vars. "
        "If the plan doesn't cover something, say so."
    ),
    model="gpt-5.4",
    model_settings=ModelSettings(max_tokens=16384),
    tools=[
        get_startup_plan,
        get_app_startup_plan,
        recompute_startup_plan,
        list_files,
        read_file,
        get_dependencies,
        search_indexed,
        ask_user,
    ],
    handoffs=[explainer_agent, tracer_agent],
    handoff_description=(
        "Hand off to the bootstrap agent for any question about getting the project "
        "running locally: install commands, env vars, required services, dependencies, "
        "Docker setup, dev-server startup, or 'how do I run this' (single repo or whole stack)."
    ),
)

boundary_extractor_agent = Agent[BoundaryReport](
    name="BoundaryExtractor",
    instructions=(
        "You produce a strict BoundaryReport for one repository. The repo's local path "
        "and indexed repo_url are in the developer prompt, alongside the per-repo startup "
        "plan that has already been generated.\n"
        "Use the startup plan as context — runtime, package manager, env vars are already "
        "known there. Your job: surface WIRE BOUNDARIES — what HTTP routes the repo "
        "exposes, what HTTP/DB endpoints it consumes, what dev-server proxies are "
        "configured, what infra services are required.\n"
        "For each consumed entry, surface BOTH the symbolic env var name AND any resolved "
        "value you can find (.env, .env.example, code defaults, docker-compose, deployment "
        "configs). Symbolic-only targets go into `ambiguities`.\n"
        "DO NOT invent paths or routes — only what you can ground in real files. Use "
        "list_files, read_file, get_dependencies, search_code, search_indexed to "
        "investigate. Once you have a complete report, return it."
    ),
    output_type=BoundaryReport,
    model="gpt-5.4",
    model_settings=ModelSettings(max_tokens=16384),
    tools=[list_files, read_file, get_dependencies, search_code, search_indexed],
)


consolidator_agent = Agent[Any](
    name="Consolidator",
    instructions=(
        "You produce a final, ordered startup plan for an application that spans "
        "multiple repos. You receive (in the developer prompt): the dependency "
        "graph (typed nodes + edges + topological order + cycle-break records), "
        "matcher-flagged ambiguities, orchestration findings, and the list of "
        "repos with their local paths and indexed urls.\n"
        "Use the lookup tools (`get_repo_boundaries`, `get_repo_startup_plan`) to "
        "read each repo's data. Use `list_files`, `read_file`, `search_indexed` "
        "to verify cross-repo claims when the matcher's confidence is low.\n"
        "DO NOT silently override the matcher's topo order — if you change it, "
        "explain why in Caveats.\n"
        "Output a single markdown document with these sections in order:\n"
        "  # Startup plan: <app name or repo set summary>\n"
        "  ## Prerequisites — required infra (postgres, redis, etc.) and version requirements\n"
        "  ## Env vars — grouped by repo, marked required/optional\n"
        "  ## Steps — one numbered step per ordered group from the topo sort, parallel commands grouped\n"
        "  ## Dependency graph — Mermaid diagram of nodes and typed edges\n"
        "  ## Caveats — ambiguous edges, cycle-breaking decisions, low-confidence matches, anything to verify"
    ),
    model="gpt-5.4",
    model_settings=ModelSettings(max_tokens=16384),
    tools=[
        get_repo_boundaries,
        get_repo_startup_plan,
        list_files,
        read_file,
        search_indexed,
    ],
)


router_agent = Agent[Any](
    name="Router",
    instructions=(
        "You are a router to route the users question to the appropriate agent, you can hand off to the explorer agent to find things in the codebase, the explainer agent to summarise and synthesise information, the tracer agent to trace the execution path of the codebase, or the bootstrap agent for questions about getting the repo running locally. After handing off to one agent you can hand off to another agent. You should ensure you completely and directly answer the users question and pick up all information from the previous agents/related to the question.\n"
        "Any question about getting the repo running locally (install, env vars, services, dev-server, Docker setup, 'how do I run this', 'how do I run the whole stack') goes to the bootstrap agent.\n"
        "Any git-related question (commit history, when/why something changed, recent changes, who/what touched a file, 'what changed recently') goes to the explainer agent — it is the only agent with `git_log`.\n"
        "If you believe the question is ambiguous or unfinished, you can use ask_user to clarify before proceeding. "
        "For example if they say 'trace the flow' without specifying which flow, ask which one. "
        "Or if they ask 'how does the uploading work' and there are multiple upload workflows (e.g. files from computer to server vs server to AWS), ask which upload process they mean.\n"
        "Multi-repo sessions: if the developer prompt lists more than one repo and the user's question doesn't clearly pick one (and isn't explicitly cross-stack), use ask_user to confirm which repo before handing off."
    ),
    model="gpt-5.4",
    tools=[ask_user],
    handoffs=[explorer_agent, explainer_agent, tracer_agent, bootstrap_agent],
)
