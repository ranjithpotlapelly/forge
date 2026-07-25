"""Phase 17 acceptance check: proves the graph mechanics a Chainlit button
click drives -- plan approve/edit/reject, the revise loop, and the second,
separate PR-approval gate -- work correctly when called the way the UI calls
them (PlanDecision-returning approve callables, a separate approve_pr), not
just the way the pre-Phase-17 lambda-bool smoke tests called them.

This is Python-level verification of the graph/product logic app/chainlit_app.py
sits on top of, not a browser click-through -- app/chainlit_app.py's own
_ask_plan_decision/_ask_pr_decision need a live Chainlit session (AskActionMessage
requires one) that only a real `chainlit run` + browser session provides.

Checks, each a real engine.run_task() against the throwaway forge-test-repo:
  A. Reject at the plan gate -> .data/workspace/ is byte-for-byte identical
     across two independent rejected runs (nothing was ever written).
  B. Edit once (revision feedback) then approve -> the graph actually loops
     plan -> approval_gate -> plan again, reaches commit/push, then approving
     the second, separate PR gate really calls open_pr_tool.run() (verified
     by a call-count wrapper, not assumed) -- failing cleanly since this repo's
     origin isn't a github.com URL, which proves the wiring reaches the real
     tool boundary without needing a live GitHub token.
  C. Approve the plan directly, then REJECT the PR gate -> open_pr_tool.run()
     is called ZERO times (verified the same way).

Run from the repo root:  python -m app.task_flow_ui_smoke_test
"""
from __future__ import annotations
import copy
import hashlib
import sys
from pathlib import Path
from app.config_loader import load_config
from app.wiring import build_engine, build_ingest, build_retriever
from product.approval import PlanDecision
from product.indexing import index_repo

TEST_REPO = r"C:\Users\Ranjith\AI-Space\forge-test-repo"
TASK = "Add a docstring to the greet function in math_utils.py"

def _snapshot(workspace: Path) -> dict[str, str]:
    result = {}
    for p in sorted(workspace.rglob("*")):
        if not p.is_file() or ".git" in p.relative_to(workspace).parts:
            continue
        result[str(p.relative_to(workspace))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return result

def _wrap_open_pr(engine) -> list[dict]:
    """Tracks every call to the real open_pr tool's run() -- proof, not
    assumption, that a PR-gate rejection makes zero of them."""
    calls: list[dict] = []
    original = engine.open_pr_tool.run

    def _tracking_run(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    engine.open_pr_tool.run = _tracking_run
    return calls

def main() -> int:
    base_cfg = load_config()
    cfg = copy.deepcopy(base_cfg)
    cfg["forge"]["repo_path"] = TEST_REPO
    cfg["retriever"]["collection"] = "forge_task_flow_ui_smoke_test"
    cfg["engine"]["checkpoint_path"] = None

    ingest = build_ingest(cfg)
    retriever = build_retriever(cfg)
    stats = index_repo(ingest, retriever)
    print(f"[ok] indexed {stats['symbols']} chunk(s) across {stats['files']} file(s) from the throwaway test repo")

    engine = build_engine(cfg, retriever=retriever)
    workspace = Path(cfg["tools"]["workspace"]).resolve()
    open_pr_calls = _wrap_open_pr(engine)

    # --- A: reject at the plan gate -> nothing written ----------------------
    engine.run_task(TASK, thread_id="ui-reject-a", approve=lambda plan: PlanDecision("reject"), approve_pr=lambda *_: False)
    snap_a1 = _snapshot(workspace)
    engine.run_task(TASK, thread_id="ui-reject-a2", approve=lambda plan: PlanDecision("reject"), approve_pr=lambda *_: False)
    snap_a2 = _snapshot(workspace)
    if snap_a1 != snap_a2:
        print(f"[!!] workspace differs across two independent plan-rejections: "
              f"only in first: {set(snap_a1) - set(snap_a2)}, only in second: {set(snap_a2) - set(snap_a1)}")
        return 1
    print(f"[ok] A: plan rejected -> .data/workspace/ identical across two independent runs "
          f"({len(snap_a1)} file(s) checked, .git/ excluded)")
    if open_pr_calls:
        print(f"[!!] open_pr_tool.run() was called during a plan-level rejection: {open_pr_calls}")
        return 1
    print("[ok] A: open_pr_tool.run() was called ZERO times (verified, not assumed)")

    # --- B: edit (revise) once, then approve; approve the PR gate too -------
    approve_calls = {"n": 0}
    def approve_with_one_revision(plan):
        approve_calls["n"] += 1
        if approve_calls["n"] == 1:
            return PlanDecision("edit", feedback="Mention the parameter's expected type in the docstring too.")
        return PlanDecision("approve")

    pr_gate_seen = []
    def approve_pr_yes(diff, title, body):
        pr_gate_seen.append((diff, title, body))
        return True

    result_b = engine.run_task(TASK, thread_id="ui-revise-then-approve", approve=approve_with_one_revision, approve_pr=approve_pr_yes)

    if approve_calls["n"] != 2:
        print(f"[!!] expected approve() to be called exactly twice (edit, then approve), got {approve_calls['n']}")
        return 1
    print("[ok] B: approve() called twice — the graph looped plan -> approval_gate -> plan after 'edit'")

    if result_b.get("approved") is not True:
        print(f"[!!] expected approved=True after the second approve() call, got {result_b.get('approved')!r}")
        return 1
    commit_result = result_b.get("commit_result")
    push_result = result_b.get("push_result")
    if not commit_result or commit_result.get("status") != "ok":
        print(f"[!!] expected a successful commit_result, got {commit_result!r}")
        return 1
    if not push_result or push_result.get("status") != "ok":
        print(f"[!!] expected a successful push_result, got {push_result!r}")
        return 1
    print(f"[ok] B: commit_result={commit_result}, push_result={push_result}")

    if not pr_gate_seen:
        print("[!!] approve_pr() was never called — pr_approval_gate_node did not run")
        return 1
    diff, title, body = pr_gate_seen[0]
    if not diff.strip():
        print("[!!] expected a non-empty diff shown to the PR-approval gate")
        return 1
    print(f"[ok] B: PR-approval gate showed a real diff ({len(diff)} chars), title={title!r}")

    if len(open_pr_calls) != 1:
        print(f"[!!] expected open_pr_tool.run() called exactly once after approving the PR gate, got {len(open_pr_calls)}")
        return 1
    print(f"[ok] B: open_pr_tool.run() called exactly once, with head={open_pr_calls[0].get('head')!r} "
          f"(verified, not assumed)")

    # This repo has no GITHUB_TOKEN and no github.com origin, so open_pr can't
    # actually succeed here -- OpenPrTool checks the token before it even
    # looks at the origin, so whichever of the two is missing is the error
    # that surfaces. Either is a clean, expected failure that proves the
    # wiring reached the real tool boundary; a real token + a github.com
    # remote is what it'd take to see this succeed end to end.
    pr_result = result_b.get("pr_result")
    detail = (pr_result or {}).get("detail", "")
    if not pr_result or pr_result.get("status") != "error" or not ("GITHUB_TOKEN" in detail or "github.com" in detail):
        print(f"[!!] expected open_pr to fail cleanly (no token / no GitHub remote here), got {pr_result!r}")
        return 1
    print(f"[ok] B: open_pr failed with a clear, expected error (no real GitHub token/remote in this environment): {detail}")

    # --- C: approve the plan directly, then REJECT the PR gate --------------
    open_pr_calls.clear()
    result_c = engine.run_task(
        TASK, thread_id="ui-reject-pr-gate",
        approve=lambda plan: PlanDecision("approve"),
        approve_pr=lambda diff, title, body: False,
    )
    if result_c.get("pr_approved") is not False:
        print(f"[!!] expected pr_approved=False, got {result_c.get('pr_approved')!r}")
        return 1
    if "pr_result" in result_c:
        print(f"[!!] open_pr should never have run after a PR-gate rejection, got pr_result={result_c['pr_result']!r}")
        return 1
    if open_pr_calls:
        print(f"[!!] open_pr_tool.run() was called during a PR-gate rejection: {open_pr_calls}")
        return 1
    print("[ok] C: PR-gate rejected -> pr_approved=False, open_pr_tool.run() called ZERO times (verified, not assumed)")

    print("\nPhase 17 (Chainlit task-flow UI, engine-level) OK. Plan approve/edit/reject, the revise "
          "loop, and the second PR-approval gate all behave correctly under the PlanDecision contract "
          "app/chainlit_app.py's action buttons drive.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
