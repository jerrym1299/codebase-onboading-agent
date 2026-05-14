import os
from dataclasses import dataclass

from services.repo_manifest import RepoManifest

MAX_LINE_CHARS = 20000


@dataclass
class RepoTextLine:
    file_path: str
    file_sha256: str
    language: str | None
    line_number: int
    line_text: str


def build_text_lines(repo_dir: str, manifest: RepoManifest) -> list[RepoTextLine]:
    """Build a line-level exact-search inventory from a repo manifest."""
    rows: list[RepoTextLine] = []
    for file in manifest.files:
        abs_path = os.path.join(repo_dir, file.file_path)
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    line_text = raw_line.rstrip("\r\n")
                    if not line_text.strip():
                        continue
                    if len(line_text) > MAX_LINE_CHARS:
                        line_text = line_text[:MAX_LINE_CHARS]
                    rows.append(RepoTextLine(
                        file_path=file.file_path,
                        file_sha256=file.file_sha256,
                        language=file.language,
                        line_number=line_number,
                        line_text=line_text,
                    ))
        except OSError:
            continue
    return rows
