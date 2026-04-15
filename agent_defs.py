from typing import Any


from temporalio import activity
from agents import Agent, Runner
from services.tools import list_files, search_code, read_file

explorer_agent = Agent[Any](
    name = "Explorer",
    instructions = "You are an assistant to explore the codebase. Your job is to find things like functions, classes, files and functionality.",
    model="gpt-4o-mini",
    tools = [list_files, search_code, read_file]
)

