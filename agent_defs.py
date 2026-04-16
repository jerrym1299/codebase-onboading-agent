from typing import Any

from agents import Agent, ModelSettings, handoff
from services.tools import (
    list_files, search_code, read_file,
    find_references, get_dependencies, search_indexed, search_dir_summaries, git_log,
)

#Explorer agent finds exact matches in the codebase.
# - "where is <filename>" → use list_files with glob "**/<filename>" and return just the file path(s).
# - "where is <symbol>" (function/class/variable) → use search_code and return file:line matches.


tracer_agent = Agent[Any](
    name="Tracer",
    instructions=(
        "You are an assistant to trace the execution path of the codebase. You are given a file:line and you need to trace the execution path of the codebase."
    ),
    model="gpt-4.1-mini",
    model_settings=ModelSettings(max_tokens=16384),
    tools=[read_file, find_references, get_dependencies],
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
        "search_code and respond with file:line matches. \n"
        "3. Use read_file only when the user wants the contents of a specific file or range.\n"
        "Keep responses concise — return paths or file:line entries, not prose summaries, unless asked."
    ),
    model="gpt-4.1-mini",
    model_settings=ModelSettings(max_tokens=16384),
    tools=[list_files, search_code, read_file],
    handoff_description="Hand off to the explorer agent to find things in the codebase, give paths/file:lines, to search for specific functionality, symbols, classes or functions",
)

explainer_agent = Agent[Any](
    name="Explainer",
    instructions=(
        "You explain and summarise a codebase.\n"
        "You receive two values in the prompt: a local path and an indexed repo_url.\n"
        "- Use `search_indexed(query, repo_url, k)` with the GitHub-style repo_url (e.g. "
        "'https://github.com/org/name'). Do NOT pass the local path there.\n"
        "- Use `list_files(dir_path, glob)` to discover real files before reading them. "
        "Never invent paths.\n"
        "- Use `read_file(file_path, ...)` only on concrete files returned by list_files or "
        "search_indexed — queries the indexed chunks in the database, use it for semantic search and finding specific code chunks. \n"
        "Cite file paths and line ranges in your answer."
    ),
    model="gpt-4.1-mini",
    model_settings=ModelSettings(max_tokens=16384),
    tools=[search_indexed, search_dir_summaries, list_files, read_file, git_log],
    handoff_description="Hand off to the explainer agent to summarise/synthesise information and answer 'explain X' or 'how does X work' questions.",
)

router_agent = Agent[Any](
    name="Router",
    instructions=(
        "You are a router to route the users question to the appropriate agent, you can hand off to the explorer agent to find things in the codebase, the explainer agent to summarise and synthesise information, or the tracer agent to trace the execution path of the codebase. After handing off to one agent you can hand off to another agent. You should ensure you completely and directly answer the users question and pick up all information from the previous agents/related to the question."
    ),
    model="gpt-4.1-mini",
    handoffs=[explorer_agent, explainer_agent, tracer_agent],
)
