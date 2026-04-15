from typing import Any

from agents import Agent
from services.tools import list_files, search_code, read_file

#Explorer agnet finds exact matches in the codebase and returns the file:line number of the matches
explorer_agent = Agent[Any](
    name="Explorer",
    instructions="You are an assistant to explore the codebase. Your job is to find things like functions, classes, files and functionality.",
    model="gpt-4o-mini",
    tools=[list_files, search_code, read_file],
)
