"""Local MCP server (Phase 7): filesystem tools scoped to a sandbox workspace
directory. Run as a subprocess over stdio by adapters/tools_mcp.py — never
imported directly by the rest of Forge.
"""
from __future__ import annotations
import sys
from pathlib import Path
from mcp.server.fastmcp import FastMCP

WORKSPACE = Path(sys.argv[1] if len(sys.argv) > 1 else "./.data/workspace").resolve()
WORKSPACE.mkdir(parents=True, exist_ok=True)

mcp = FastMCP("forge-workspace")

def _resolve(path: str) -> Path:
    target = (WORKSPACE / path).resolve()
    if target != WORKSPACE and WORKSPACE not in target.parents:
        raise ValueError(f"{path!r} escapes the workspace sandbox ({WORKSPACE})")
    return target

@mcp.tool()
def read_file(path: str) -> str:
    """Read a text file from the sandboxed workspace."""
    return _resolve(path).read_text(encoding="utf-8")

@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Write a text file into the sandboxed workspace. Mutates the outside
    world, so Forge gates this behind human approval (config.yaml's
    forge.require_approval_for)."""
    target = _resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} bytes to {path}"

if __name__ == "__main__":
    mcp.run(transport="stdio")
