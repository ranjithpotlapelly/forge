"""Citation source lines (Phase 16): reads an exact line range from disk for
display -- e.g. an expanded citation in the Chainlit UI.

Deliberately reads fresh from disk on every call rather than trusting a
Chunk's indexed/embedded content, which is a copy taken at index time and can
drift from the file's current contents. Scoped to a repo root the same way
adapters/mcp_servers/workspace_server.py scopes writes to its sandbox, so a
crafted path metadata value can't read outside the indexed repo.
"""
from __future__ import annotations
from pathlib import Path

def read_lines(repo_path: str | Path, path: str, start_line: int, end_line: int) -> str:
    root = Path(repo_path).resolve()
    target = (root / path).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"{path!r} escapes the repo root ({root})")
    lines = target.read_text(encoding="utf-8").splitlines()
    start = max(start_line, 1)
    end = min(end_line, len(lines))
    return "\n".join(lines[start - 1:end])
