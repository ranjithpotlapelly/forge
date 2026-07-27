"""Local MCP server (Phase 7/12/13): filesystem + git + test-runner tools
scoped to a sandbox workspace directory. Run as a subprocess over stdio by
adapters/tools_mcp.py — never imported directly by the rest of Forge.
"""
from __future__ import annotations
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from mcp.server.fastmcp import FastMCP

WORKSPACE = Path(sys.argv[1] if len(sys.argv) > 1 else "./.data/workspace").resolve()
WORKSPACE.mkdir(parents=True, exist_ok=True)

def _rmtree_force(path: Path) -> None:
    # git marks object files read-only; Windows (unlike POSIX) blocks
    # deleting a read-only file even with directory write permission, so a
    # plain shutil.rmtree fails on any real .git dir with WinError 5.
    def _on_error(func, p, exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onexc=_on_error)

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

# marker file -> steps, checked in this order. Each step is run in sequence;
# the first non-zero exit stops the run there (its output is what's reported).
# The pytest command runs via `sys.executable -m pytest` rather than a bare
# "pytest" binary lookup: this server's own subprocess inherits its caller's
# PATH, which doesn't include the venv's Scripts/ dir when the venv isn't
# "activated" (true of how this whole project has been run) — `-m` guarantees
# the same interpreter this server itself runs under, so an installed pytest
# is always found.
# npm gets two steps, not one: `node_modules/` is gitignored in virtually
# every JS/TS repo, so a fresh clone never has it — `npm test` alone would
# just fail with "ng/jest/etc: not found". `npm ci` (not `npm install`)
# because it's the one that requires and trusts package-lock.json exactly,
# which is what CI/sandboxed runs should do.
_BUILD_COMMANDS: list[tuple[str, list[list[str]]]] = [
    ("pom.xml", [["mvn", "-q", "test"]]),
    ("build.gradle", [["gradle", "test"]]),
    ("pytest.ini", [[sys.executable, "-m", "pytest", "-q"]]),
    ("pyproject.toml", [[sys.executable, "-m", "pytest", "-q"]]),
    # `--browsers=ChromeHeadless` isn't in here: it's Karma-specific and not
    # every npm test setup recognizes it (or even has a schema for the "test"
    # target at all — that's a per-project config problem, not this server's
    # to solve). `--watch=false` is the one flag that's near-universal for a
    # non-interactive CI run (Angular/Karma, Jest, etc. all honor it).
    ("package.json", [["npm", "ci"], ["npm", "test", "--", "--watch=false"]]),
]

_MAX_OUTPUT_CHARS = 4000

# Skipped when searching one level down for a marker: never the actual
# project root, and node_modules/.git can be enormous -- iterdir()-ing
# their contents for a marker check would be pure waste.
_SEARCH_EXCLUDED_DIRS = {".git", "node_modules", "dist", "build", "target", "__pycache__", ".venv", "venv"}

def _detect_build_steps() -> tuple[list[list[str]], Path]:
    """Returns (steps, cwd) -- cwd is WORKSPACE itself, or one level down,
    whichever directory the marker was actually found in. repo_path (hence
    WORKSPACE, its clone) doesn't always coincide with a JS/TS/Java project's
    own root: e.g. this repo's real upstream structure is a wrapper
    directory containing the Angular app one level down, alongside an
    unrelated top-level file or two -- searching only WORKSPACE's immediate
    root would never find package.json there."""
    for marker, steps in _BUILD_COMMANDS:
        if (WORKSPACE / marker).exists():
            return steps, WORKSPACE
    for child in sorted(WORKSPACE.iterdir()):
        if not child.is_dir() or child.name in _SEARCH_EXCLUDED_DIRS:
            continue
        for marker, steps in _BUILD_COMMANDS:
            if (child / marker).exists():
                return steps, child
    markers = ", ".join(m for m, _ in _BUILD_COMMANDS)
    raise ValueError(f"no recognized build system in {WORKSPACE} or its immediate subdirectories (looked for: {markers})")

@mcp.tool()
def run_tests(timeout_s: int = 300) -> str:
    """Run the workspace's test suite (auto-detects Maven/Gradle/pytest/npm
    from pom.xml/build.gradle/pytest.ini/pyproject.toml/package.json, checked
    in WORKSPACE's root and, failing that, its immediate subdirectories).
    Read-only in effect — runs inside the workspace sandbox only and changes
    nothing outside it — so Forge does not gate this behind human approval.
    Returns a JSON string: {passed, exit_code, output, duration_s}. `output`
    is stdout+stderr (across every step run, for npm's install-then-test
    sequence), truncated to the last ~4000 chars so a full Maven/Gradle/npm
    run doesn't blow a model's context window when fed into a retry prompt.
    `timeout_s` applies per step, not to the whole sequence."""
    steps, cwd = _detect_build_steps()
    start = time.monotonic()
    output_parts: list[str] = []
    exit_code = 0
    for step in steps:
        try:
            result = subprocess.run(
                step, cwd=cwd, capture_output=True, text=True,
                timeout=timeout_s, stdin=subprocess.DEVNULL,
            )
            exit_code = result.returncode
            output_parts.append(result.stdout + result.stderr)
        except subprocess.TimeoutExpired as e:
            exit_code = -1
            output_parts.append((e.stdout or "") + (e.stderr or "") + f"\n[timed out after {timeout_s}s]")
        if exit_code != 0:
            break
    output = "\n".join(output_parts)
    duration_s = round(time.monotonic() - start, 2)
    if len(output) > _MAX_OUTPUT_CHARS:
        output = output[-_MAX_OUTPUT_CHARS:]
    return json.dumps({
        "passed": exit_code == 0,
        "exit_code": exit_code,
        "output": output,
        "duration_s": duration_s,
    })

@mcp.tool()
def prepare_workspace(repo_path: str, branch_prefix: str = "forge/task") -> str:
    """Clone the indexed source repo into the workspace on a fresh branch, so
    a plan's target_path values (relative to the source repo root) resolve
    1:1 here. DESTRUCTIVE: wipes whatever was previously in the workspace
    sandbox first — a full fresh clone, not an in-place sync. Fails fast if
    the source repo has uncommitted changes. Not gated behind approval: it
    only ever reads the source repo and writes into the sandbox, never the
    reverse."""
    source = Path(repo_path).resolve()
    if not (source / ".git").exists():
        raise ValueError(f"{source} is not a git repository")

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=source, capture_output=True,
        text=True, timeout=30, stdin=subprocess.DEVNULL,
    )
    if status.returncode != 0:
        raise RuntimeError(f"git status failed in {source}: {status.stderr.strip()}")
    if status.stdout.strip():
        raise ValueError(
            f"{source} has uncommitted changes — commit or stash before starting a task:\n"
            f"{status.stdout.strip()}"
        )

    if WORKSPACE.exists():
        _rmtree_force(WORKSPACE)
    clone = subprocess.run(
        ["git", "clone", str(source), str(WORKSPACE)], capture_output=True,
        text=True, timeout=120, stdin=subprocess.DEVNULL,
    )
    if clone.returncode != 0:
        raise RuntimeError(f"git clone failed: {clone.stderr.strip()}")

    branch = f"{branch_prefix}-{int(time.time())}"
    _run_git("checkout", "-b", branch)
    # Fresh clone has no local identity of its own yet.
    _run_git("config", "user.name", "Forge Agent")
    _run_git("config", "user.email", "forge-agent@localhost")
    return json.dumps({"branch": branch, "source": str(source), "workspace": str(WORKSPACE)})

if __name__ == "__main__":
    mcp.run(transport="stdio")
