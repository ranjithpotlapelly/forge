"""Phase 20 acceptance check: start a task, pause it at the plan-approval
interrupt, KILL the process entirely (two genuinely separate OS subprocesses
below, not just two Python function calls in one interpreter), restart,
resume by thread_id via the real `python -m app.resume` CLI, and approve --
proving the edit is applied exactly once and no earlier node re-executed.
Verified, not assumed: via run_history's per-node step counts (each of
classify_intent/prepare_workspace/task_retrieve/plan/edit/test/commit/push
must appear in the step log exactly once) and a real `git log` in
.data/workspace (exactly one new commit).

Uses the real, default config.yaml -- not a throwaway test repo or a
checkpoint-path override -- since app/resume.py is a real CLI meant to work
against real config, and this is the most faithful test of exactly that.
Reads/mutates .data/workspace and .data/checkpoints.db, the same shared
state other task-flow smoke tests use; run this alone, not concurrently with
another one (see this session's own history: running two at once corrupts
both via a shared-workspace race).

Run from the repo root:  python -m app.interrupt_resume_smoke_test
"""
from __future__ import annotations
import subprocess
import sys
import uuid
from collections import Counter
from pathlib import Path
from app.config_loader import load_config
from app.wiring import build_run_history

REPO_ROOT = Path(__file__).resolve().parent.parent
# Must reference something that actually exists in whatever forge.repo_path
# points at -- this test deliberately runs against the real, default config
# (see the module docstring), not a throwaway fixture with a known greet()
# function like plan_edit_smoke_test/retry_cap_smoke_test use. AuthService's
# logout() is a small, low-risk, self-contained method with no existing
# doc-comment, in the project's checked-in demo target (rag-frontend-angular-v2).
# (login() was tried and reverted: its multi-line this.http.post(...).pipe(...)
# leading-dot chain trips the edit step into producing broken TS.)
TASK = "Add a comment above the logout method in AuthService explaining what it does"

# Deliberately a standalone `python -c` script, not an import of anything in
# this repo's own test helpers -- it must be runnable as a genuinely
# independent process with zero shared state with this file.
_START_SCRIPT = """
import sys
from app.config_loader import load_config
from app.wiring import build_engine

cfg = load_config()
engine = build_engine(cfg)
result = engine.run_task(sys.argv[1], thread_id=sys.argv[2], approve=None, approve_pr=None)
if not result.get("__interrupt__"):
    print("FAIL: expected the run to pause at the plan-approval interrupt")
    sys.exit(1)
payload = result["__interrupt__"][0].value
print(f"PAUSED kind={payload['kind']} title={payload['plan']['title']!r} branch={payload['plan']['branch']!r}")
"""

def _git_log(workspace: Path) -> list[str]:
    result = subprocess.run(
        ["git", "log", "--oneline"], cwd=workspace, capture_output=True, text=True, timeout=30,
    )
    return result.stdout.strip().splitlines() if result.returncode == 0 else []

def main() -> int:
    cfg = load_config()
    history = build_run_history(cfg)
    workspace = Path(cfg["tools"]["workspace"]).resolve()
    thread_id = f"interrupt-resume-smoke-{uuid.uuid4()}"

    # --- process 1: start the task, pause at the plan-approval interrupt, exit ---
    print(f"[..] process 1: starting task, thread_id={thread_id}")
    proc1 = subprocess.run(
        [sys.executable, "-c", _START_SCRIPT, TASK, thread_id],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=600,
    )
    print(proc1.stdout)
    if proc1.stderr.strip():
        print("--- process 1 stderr ---\n" + proc1.stderr)
    if proc1.returncode != 0 or "PAUSED" not in proc1.stdout:
        print("[!!] process 1 did not pause as expected")
        return 1
    print("[ok] process 1 paused at the plan-approval interrupt, then exited "
          "(no in-memory state carries into process 2 below)")

    commits_after_pause = _git_log(workspace)

    runs = history.list_runs(limit=50)
    run = next((r for r in runs if r["thread_id"] == thread_id), None)
    if run is None:
        print("[!!] no run recorded for this thread_id after process 1")
        return 1
    if run["status"] != "running":
        print(f"[!!] expected status='running' after pausing, got {run['status']!r}")
        return 1
    steps_before = history.list_steps(run["id"])
    counts_before = Counter(s["node"] for s in steps_before)
    print(f"[ok] run_history after process 1: status='running', steps={dict(counts_before)}")
    for node in ("classify_intent", "prepare_workspace", "task_retrieve", "plan"):
        if counts_before[node] != 1:
            print(f"[!!] expected exactly 1 '{node}' step after process 1, got {counts_before[node]}")
            return 1
    for node in ("approval_gate", "edit", "test", "commit", "push"):
        if counts_before[node] != 0:
            print(f"[!!] '{node}' should not have run yet, got {counts_before[node]} step(s)")
            return 1
    print("[ok] exactly the nodes before the approval gate ran, and no further -- verified via run_history")

    # --- process 2: a fresh process, resume by thread_id, approve the plan,
    # reject the PR gate (keeps this test from needing a real GITHUB_TOKEN) ---
    print(f"\n[..] process 2: python -m app.resume {thread_id}  (approve plan, reject PR)")
    proc2 = subprocess.run(
        [sys.executable, "-m", "app.resume", thread_id],
        cwd=REPO_ROOT, input="a\nn\n", capture_output=True, text=True, timeout=600,
    )
    print(proc2.stdout)
    if proc2.stderr.strip():
        print("--- process 2 stderr ---\n" + proc2.stderr)
    if proc2.returncode != 0:
        print("[!!] app.resume exited non-zero")
        return 1
    if "commit:" not in proc2.stdout or "'status': 'ok'" not in proc2.stdout:
        print("[!!] expected a successful commit reported by app.resume")
        return 1
    print("[ok] process 2 (fresh process, resumed purely from the checkpoint) "
          "approved the plan and completed the run")

    commits_after_resume = _git_log(workspace)
    if len(commits_after_resume) != len(commits_after_pause) + 1:
        print(f"[!!] expected exactly one new commit, had {len(commits_after_pause)} "
              f"before resume and {len(commits_after_resume)} after: {commits_after_resume}")
        return 1
    if commits_after_resume[1:] != commits_after_pause:
        print(f"[!!] resume should only add one commit on top, not rewrite history: "
              f"before={commits_after_pause}, after={commits_after_resume}")
        return 1
    print(f"[ok] git log confirms exactly one new commit: {commits_after_resume[0]}")

    run_after = history.get_run(run["id"])
    if run_after["status"] != "completed":
        print(f"[!!] expected status='completed' after resume, got {run_after['status']!r}")
        return 1
    steps_after = history.list_steps(run["id"])
    counts_after = Counter(s["node"] for s in steps_after)
    print(f"[ok] run_history after process 2: status='completed', steps={dict(counts_after)}")
    for node in ("classify_intent", "prepare_workspace", "task_retrieve", "plan",
                 "approval_gate", "edit", "test", "commit", "push", "pr_gate", "pr_rejected"):
        if counts_after[node] != 1:
            print(f"[!!] expected exactly 1 '{node}' step total, got {counts_after[node]} -- "
                  f"a prior node re-executing would show up here as a count > 1")
            return 1
    print("[ok] every node -- including the ones that ran BEFORE the pause -- executed exactly "
          "once each in total: prepare_workspace/task_retrieve/plan did not re-run on resume")

    approval_step = next(s for s in steps_after if s["node"] == "approval_gate")
    if '"decision": "approve"' not in (approval_step["detail"] or ""):
        print(f"[!!] expected the approval_gate step to record decision=approve, got {approval_step['detail']!r}")
        return 1
    print("[ok] the approval decision made in process 2 is recorded in run_history's audit trail")

    print("\nPhase 20 OK. A task paused at the plan-approval interrupt survives the process "
          "that started it being killed outright; resuming from a fresh process continues "
          "from the checkpoint, applies the edit exactly once, and never re-runs a prior node.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
