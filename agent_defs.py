from typing import Any

from agents import Agent, ModelSettings, handoff
from services.tools import (
    list_files, search_code, read_file,
    find_references, get_dependencies, search_indexed, search_dir_summaries, git_log,
    ask_user,
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
        "what a file imports, and `read_file` to inspect specific ranges."
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
        "Cite file paths and line ranges in your answer."
    ),
    model="gpt-5.4",
    model_settings=ModelSettings(max_tokens=16384),
    tools=[search_indexed, search_dir_summaries, list_files, read_file, git_log, ask_user],
    handoffs=[tracer_agent],
    handoff_description="Hand off to the explainer agent to summarise/synthesise information and answer 'explain X' or 'how does X work' questions.",
)

router_agent = Agent[Any](
    name="Router",
    instructions=(
        "You are a router to route the users question to the appropriate agent, you can hand off to the explorer agent to find things in the codebase, the explainer agent to summarise and synthesise information, or the tracer agent to trace the execution path of the codebase. After handing off to one agent you can hand off to another agent. You should ensure you completely and directly answer the users question and pick up all information from the previous agents/related to the question.\n"
        "Any git-related question (commit history, when/why something changed, recent changes, who/what touched a file, 'what changed recently') goes to the explainer agent — it is the only agent with `git_log`.\n"
        "If you believe the question is ambiguous or unfinished, you can use ask_user to clarify before proceeding. "
        "For example if they say 'trace the flow' without specifying which flow, ask which one. "
        "Or if they ask 'how does the uploading work' and there are multiple upload workflows (e.g. files from computer to server vs server to AWS), ask which upload process they mean."
    ),
    model="gpt-5.4",
    tools=[ask_user],
    handoffs=[explorer_agent, explainer_agent, tracer_agent],
)
