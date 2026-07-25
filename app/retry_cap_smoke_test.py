"""Phase 14 acceptance check: proves the edit/test retry loop caps at exactly
3 attempts and then stops — never runs forever — using a target repo with a
deliberately unpassable test, so this is deterministic regardless of whether
the model's generated code is any good.

Uses its own throwaway git repo (forge-test-repo-failing), separate from the
one app/plan_edit_smoke_test.py uses, specifically so that script's assertions
(which expect the edit to survive) aren't entangled with this one's (which
expects it to be rolled back). Run from the repo root:
  python -m app.retry_cap_smoke_test
"""
from __future__ import annotations
import copy
import sys
from pathlib import Path
from app.config_loader import load_config
from app.wiring import build_engine, build_ingest, build_retriever
from product.approval import PlanDecision
from product.indexing import index_repo

TEST_REPO = r"C:\Users\Ranjith\AI-Space\forge-test-repo-failing"
TEST_CHECKPOINT_PATH = "./.data/checkpoints_retry_cap_smoke_test.db"

def main() -> int:
    base_cfg = load_config()
    cfg = copy.deepcopy(base_cfg)
    cfg["forge"]["repo_path"] = TEST_REPO
    cfg["retriever"]["collection"] = "forge_retry_cap_smoke_test"

    ingest = build_ingest(cfg)
    retriever = build_retriever(cfg)
    stats = index_repo(ingest, retriever)
    print(f"[ok] indexed {stats['symbols']} chunk(s) across {stats['files']} file(s) from the throwaway (always-failing-tests) repo")

    # Phase 20: approval_gate now uses interrupt(), which requires a real checkpointer.
    Path(TEST_CHECKPOINT_PATH).unlink(missing_ok=True)
    cfg["engine"]["checkpoint_path"] = TEST_CHECKPOINT_PATH
    engine = build_engine(cfg, retriever=retriever)
    workspace = Path(cfg["tools"]["workspace"]).resolve()

    result = engine.run_task(
        "Add a null check at the start of greet that raises ValueError if name is None",
        thread_id="retry-cap-smoke",
        approve=lambda plan: PlanDecision("approve"),
    )

    if result.get("approved") is not True:
        print(f"[!!] expected approved=True, got {result.get('approved')!r}")
        return 1

    history = result.get("test_history", [])
    print(f"\n=== test_history: {len(history)} attempt(s) ===")
    for entry in history:
        print(f"  attempt {entry['attempt']}: passed={entry['passed']} exit_code={entry['exit_code']}")

    if len(history) != 3:
        print(f"[!!] expected exactly 3 test attempts, got {len(history)}: {history}")
        return 1
    if any(entry["passed"] for entry in history):
        print(f"[!!] expected every attempt to fail (the test is deliberately unpassable), got {history}")
        return 1
    print("[ok] exactly 3 test attempts, all failed as expected")

    if result.get("attempt") != 3:
        print(f"[!!] expected final attempt counter == 3, got {result.get('attempt')!r}")
        return 1
    print(f"[ok] attempt counter stopped at exactly 3 (not looping forever)")

    if result.get("gave_up") is not True:
        print(f"[!!] expected gave_up=True, got {result.get('gave_up')!r}")
        return 1
    if "commit_result" in result:
        print(f"[!!] should never reach commit after giving up, got commit_result={result['commit_result']!r}")
        return 1
    print("[ok] gave_up=True, commit never ran")

    rolled_back = result.get("rolled_back_paths", [])
    if "math_utils.py" not in rolled_back:
        print(f"[!!] expected math_utils.py to be rolled back, got {rolled_back!r}")
        return 1
    print(f"[ok] rolled_back_paths: {rolled_back}")

    final_content = (workspace / "math_utils.py").read_text(encoding="utf-8")
    if final_content.strip() != 'def greet(name):\n    return f"Hello, {name}!"'.strip():
        print(f"[!!] expected the file restored to its original content, got:\n{final_content}")
        return 1
    print(f"[ok] math_utils.py content matches the pre-edit original after rollback:\n{final_content}")

    print("\nPhase 14 (retry cap) OK. Retried exactly 3 times against an unpassable test, then stopped and rolled back.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
