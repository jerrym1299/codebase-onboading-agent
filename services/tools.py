from pathlib import Path
import re
from agents import function_tool


def _list_files(dir_path: str, glob: str = "**/*") -> list[str]:
    return [str(p) for p in Path(dir_path).glob(glob) if p.is_file()]


def _read_file(file_path: str, start_line: int = 0, end_line: int = -1) -> str:
    with open(file_path, "r", errors="replace") as f:
        lines = f.readlines()
    return "".join(lines[start_line:] if end_line == -1 else lines[start_line:end_line])


@function_tool
def list_files(dir_path: str, glob: str = "**/*") -> list[str]:
    return _list_files(dir_path, glob)


@function_tool
def read_file(file_path: str, start_line: int = 0, end_line: int = -1) -> str:
    return _read_file(file_path, start_line, end_line)


@function_tool
def search_code(dir_path: str, query: str, file_type: str = "") -> list[str]:
    """Search code with regex or string. Returns 'file:line_number' for matches."""
    files = _list_files(dir_path, glob=f"**/*{file_type}" if file_type else "**/*")
    pattern = re.compile(query)
    results = []
    for file in files:
        try:
            content = _read_file(file)
        except (UnicodeDecodeError, OSError):
            continue
        for line_num, line in enumerate(content.splitlines(), start=1):
            if pattern.search(line):
                results.append(f"{file}:{line_num}")
    return results
