"""Phase 12 smoke test: proves commit/push are real git operations, gated by
the same approval mechanism as write_file, and that push actually moves
history to a remote — a local throwaway bare repo, never anything hosted, so
this stays fully local with zero external side effects.

Checks: (1) both tools require approval per config, (2) a denied commit
leaves the workspace repo history empty, an approved one creates a real
commit, (3) a denied push never reaches the remote, an approved one does.
Run from the repo root:  python -m app.git_tools_smoke_test
"""
from __future__ import annotations
import shutil
import subprocess
import sys
from pathlib import Path
from app.config_loader import load_config
from app.wiring import build_tools
from product.approval import ApprovalDenied, run_tool

def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()

def _log_oneline(cwd: Path, ref: str = "") -> str:
    """git log --oneline, but "no commits yet" means empty history, not an error."""
    try:
        return _git("log", "--oneline", *([ref] if ref else []), cwd=cwd)
    except RuntimeError as e:
        if "does not have any commits yet" in str(e) or "unknown revision" in str(e):
            return ""
        raise

def main() -> int:
    cfg = load_config()
    workspace = Path(cfg["tools"]["workspace"]).resolve()
    remote_path = workspace.parent / "workspace_remote.git"

    # Idempotency: previous runs leave a repo + remote behind.
    shutil.rmtree(workspace / ".git", ignore_errors=True)
    shutil.rmtree(remote_path, ignore_errors=True)
    (workspace / "commit_demo.txt").unlink(missing_ok=True)

    tools = {t.name: t for t in build_tools(cfg)}
    write_tool, commit_tool, push_tool = tools["write_file"], tools["commit"], tools["push"]

    if not commit_tool.requires_approval or not push_tool.requires_approval:
        print("[!!] expected both commit and push to require approval per config.yaml")
        return 1
    print("[ok] commit.requires_approval=True, push.requires_approval=True")

    run_tool(write_tool, {"path": "commit_demo.txt", "content": "hello"}, approve=lambda t, a: True)
    print("[ok] wrote commit_demo.txt (write_file already proven gated in Phase 7)")

    try:
        run_tool(commit_tool, {"message": "add commit_demo.txt"}, approve=lambda t, a: False)
        print("[!!] expected ApprovalDenied when the gate denies")
        return 1
    except ApprovalDenied:
        pass
    log = _log_oneline(workspace)
    if log:
        print(f"[!!] expected no commits after a denied commit, got: {log!r}")
        return 1
    print("[ok] denied commit -> workspace repo history is still empty")

    run_tool(commit_tool, {"message": "add commit_demo.txt"}, approve=lambda t, a: True)
    log = _log_oneline(workspace)
    if len(log.splitlines()) != 1:
        print(f"[!!] expected exactly one commit, got: {log!r}")
        return 1
    print(f"[ok] approved commit -> real commit created: {log}")

    # Local throwaway bare repo as a safe stand-in remote. Never anything hosted.
    _git("init", "--bare", "-b", "main", str(remote_path), cwd=workspace.parent)
    _git("remote", "add", "origin", str(remote_path), cwd=workspace)
    print(f"[ok] added local bare repo as 'origin' ({remote_path})")

    try:
        run_tool(push_tool, {"remote": "origin", "branch": "main"}, approve=lambda t, a: False)
        print("[!!] expected ApprovalDenied when the gate denies")
        return 1
    except ApprovalDenied:
        pass
    refs = _git("branch", "-a", cwd=remote_path)
    if refs:
        print(f"[!!] expected the remote to have no branches after a denied push, got: {refs!r}")
        return 1
    print("[ok] denied push -> remote has no branches yet")

    run_tool(push_tool, {"remote": "origin", "branch": "main"}, approve=lambda t, a: True)
    remote_log = _git("log", "--oneline", "main", cwd=remote_path)
    if remote_log != log:
        print(f"[!!] expected remote history to match local, got: {remote_log!r} vs {log!r}")
        return 1
    print(f"[ok] approved push -> remote now has the commit: {remote_log}")

    print("\nPhase 12 OK. commit/push wired, gated, and proven to move real git history.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
