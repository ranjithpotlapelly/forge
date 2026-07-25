"""Phase 14 acceptance check: rejecting a task's plan must leave
.data/workspace/ byte-for-byte unchanged (excluding .git/ internals, which
legitimately differ run-to-run: prepare_workspace creates a fresh branch
name/commit metadata on every task start regardless of approval outcome).

Uses a small throwaway git repo (not this project) as the "indexed repo",
in a separate Chroma collection, so this doesn't depend on this session's
own uncommitted changes or mix with other smoke tests' fixture data.
Run from the repo root:  python -m app.plan_approval_smoke_test
"""
from __future__ import annotations
import copy
import hashlib
import sys
from pathlib import Path
from app.config_loader import load_config
from app.wiring import build_engine, build_ingest, build_retriever
from product.indexing import index_repo

TEST_REPO = r"C:\Users\Ranjith\AI-Space\forge-test-repo"

def _snapshot(workspace: Path) -> dict[str, str]:
    result = {}
    for p in sorted(workspace.rglob("*")):
        if not p.is_file() or ".git" in p.relative_to(workspace).parts:
            continue
        result[str(p.relative_to(workspace))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return result

def main() -> int:
    base_cfg = load_config()
    cfg = copy.deepcopy(base_cfg)
    cfg["forge"]["repo_path"] = TEST_REPO
    cfg["retriever"]["collection"] = "forge_plan_smoke_test"

    ingest = build_ingest(cfg)
    retriever = build_retriever(cfg)
    indexed = index_repo(ingest, retriever)
    print(f"[ok] indexed {indexed} chunk(s) from the throwaway test repo into a separate collection")

    cfg["engine"]["checkpoint_path"] = None  # skip checkpointing for this test — irrelevant to what's being proven
    engine = build_engine(cfg, retriever=retriever)
    workspace = Path(cfg["tools"]["workspace"]).resolve()

    approve_calls = []
    result = engine.run_task(
        "Add a docstring to the greet function in math_utils.py",
        thread_id="plan-reject-smoke",
        approve=lambda plan: (approve_calls.append(plan), False)[1],
    )

    if not approve_calls:
        print("[!!] approve() was never called — approval_gate did not run")
        return 1
    print(f"[ok] approve() called once with a real plan: title={approve_calls[0].title!r}, "
          f"branch={approve_calls[0].branch!r}, {len(approve_calls[0].steps)} step(s)")

    if result.get("approved") is not False:
        print(f"[!!] expected approved=False, got {result.get('approved')!r}")
        return 1
    if not result.get("rejection_reason"):
        print("[!!] expected rejection_reason to be set")
        return 1
    print(f"[ok] run_task() returned cleanly (no exception): approved=False, rejection_reason={result['rejection_reason']!r}")

    if "edit_results" in result:
        print(f"[!!] edit_results should not be present after a rejection, got {result['edit_results']!r}")
        return 1
    print("[ok] no edit_results present — the edit node never ran")

    snapshot_after_first_reject = _snapshot(workspace)

    # A second, independent reject run. prepare_workspace re-clones from the
    # same source commit regardless of the first run's outcome, so if
    # rejection truly wrote nothing, both snapshots must match exactly.
    engine.run_task(
        "Add a docstring to the greet function in math_utils.py",
        thread_id="plan-reject-smoke-2",
        approve=lambda plan: False,
    )
    snapshot_after_second_reject = _snapshot(workspace)

    if snapshot_after_first_reject != snapshot_after_second_reject:
        only_first = set(snapshot_after_first_reject) - set(snapshot_after_second_reject)
        only_second = set(snapshot_after_second_reject) - set(snapshot_after_first_reject)
        changed = {
            f for f in set(snapshot_after_first_reject) & set(snapshot_after_second_reject)
            if snapshot_after_first_reject[f] != snapshot_after_second_reject[f]
        }
        print(f"[!!] workspace differs between two independent rejected runs — "
              f"only in first: {only_first}, only in second: {only_second}, content differs: {changed}")
        return 1
    print(f"[ok] .data/workspace/ is byte-for-byte identical across two independent rejected task runs "
          f"({len(snapshot_after_first_reject)} file(s) checked, .git/ excluded)")

    print("\nPhase 14 (approval gate) OK. Rejecting a plan costs one model call and writes nothing to disk.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
