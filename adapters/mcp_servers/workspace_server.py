"""Local MCP server (Phase 7/12): filesystem + git tools scoped to a sandbox
workspace directory. Run as a subprocess over stdio by adapters/tools_mcp.py
— never imported directly by the rest of Forge.
"""
from __future__ import annotations
import subprocess
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

def _run_git(*args: str) -> str:
    # stdin=DEVNULL matters: this server's own stdin is the MCP stdio JSON-RPC
    # channel. Without it, the child inherits that pipe and can hang the
    # transport (observed: `git add -A` blocking indefinitely).
    result = subprocess.run(
        ["git", *args], cwd=WORKSPACE, capture_output=True, text=True, timeout=30,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip() or f"git {' '.join(args)}: ok"

def _ensure_git_repo() -> None:
    # Check for .git/HEAD, not just the .git directory: a repo left partway
    # through init (e.g. an interrupted process) can have a .git dir without
    # one, and git then silently walks up to a parent repo instead of erroring.
    if (WORKSPACE / ".git" / "HEAD").exists():
        return
    # Local-only identity (no --global): this repo is Forge's own sandbox,
    # never the user's real project checkout, so it's safe to set here and
    # only here.
    _run_git("init", "-b", "main")
    _run_git("config", "user.name", "Forge Agent")
    _run_git("config", "user.email", "forge-agent@localhost")

_ensure_git_repo()

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

@mcp.tool()
def commit(message: str) -> str:
    """Stage and commit all changes in the workspace repo. Mutates local git
    history, so Forge gates this behind human approval."""
    _run_git("add", "-A")
    return _run_git("commit", "-m", message)

@mcp.tool()
def push(remote: str = "origin", branch: str = "main") -> str:
    """Push the current branch to a remote. Mutates a possibly-shared
    remote, so Forge gates this behind human approval. No remote is
    configured by default — this fails with a clear git error unless one
    has already been added (e.g. via a real `git remote add origin ...`
    against a checkout you control)."""
    return _run_git("push", remote, branch)

if __name__ == "__main__":
    mcp.run(transport="stdio")
