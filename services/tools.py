from pathlib import Path
import re
from agents import function_tool


@function_tool
def list_files(dir_path: str, glob: str = "**/*") -> list[str]:
    return [str(p) for p in Path(dir_path).glob(glob) if p.is_file()]


@function_tool
def read_file(file_path: str, start_line: int = 0, end_line: int = -1) -> str:
    with open(file_path, "r") as f:
        lines = f.readlines()
        if end_line == -1:
            return "".join(lines[start_line:])
        return "".join(lines[start_line:end_line])


@function_tool
def search_code(dir_path: str, query: str, file_type: str = "") -> list[str]:
    """Search code with regex or string. Returns 'file:line_number' for matches."""
    if file_type:
        files = list_files(dir_path, glob=f"**/*{file_type}")
    else:
        files = list_files(dir_path)

    results = []
    for file in files:
        try:
            content = read_file(file)
        except (UnicodeDecodeError, OSError):
            continue
        for line_num, line in enumerate(content.splitlines(), start=1):
            if re.search(query, line):
                results.append(f"{file}:{line_num}")
    return results
