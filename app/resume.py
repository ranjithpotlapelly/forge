"""CLI (Phase 20): continue a task run paused at an approval gate.

  python -m app.resume <thread_id>

Works from a brand new process -- state lives in the checkpointer
(.data/checkpoints.db by thread_id, per config.yaml's engine.checkpoint_path),
not in whatever process originally called run_task(). That original process
can have exited, crashed, or been killed outright; this only needs the
checkpoint file and the thread_id.
"""
from __future__ import annotations
import sys
from typing import Any
from app.config_loader import load_config
from app.wiring import build_engine
from product.approval import PlanDecision

def _prompt_plan_decision(plan: Any) -> PlanDecision:
    print("\n=== Proposed plan ===")
    print(f"Title:  {plan.title}")
    print(f"Branch: {plan.branch}")
    print("Steps:")
    for i, step in enumerate(plan.steps, 1):
        print(f"  {i}. [{step.kind}] {step.target_path} - {step.description}")
    print()
    while True:
        choice = input("Approve, Edit, or Reject this plan? [a/e/r]: ").strip().lower()
        if choice in ("a", "approve"):
            return PlanDecision("approve")
        if choice in ("r", "reject"):
            return PlanDecision("reject")
        if choice in ("e", "edit"):
            feedback = input("What should change? ").strip()
            return PlanDecision("edit", feedback=feedback or "(no feedback given)")
        print("Please enter a, e, or r.")

def _prompt_pr_decision(diff: str, title: str, body: str) -> bool:
    print("\n=== Ready to open a pull request ===")
    print(f"Title: {title}\n\nBody:\n{body}\n\nDiff:\n{diff}\n")
    while True:
        choice = input("Open this pull request? [y/n]: ").strip().lower()
        if choice in ("y", "yes"):
            return True
        if choice in ("n", "no"):
            return False
        print("Please enter y or n.")

def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python -m app.resume <thread_id>")
        return 1
    thread_id = sys.argv[1]

    cfg = load_config()
    engine = build_engine(cfg)

    try:
        result = engine.resume_task(thread_id, approve=_prompt_plan_decision, approve_pr=_prompt_pr_decision)
    except RuntimeError as e:
        print(f"Could not resume: {e}")
        return 1

    if result.get("__interrupt__"):
        print(f"\nRun {thread_id} is paused again at another approval gate. "
              f"Resume again with: python -m app.resume {thread_id}")
        return 0

    print(f"\nRun {thread_id} finished.")
    if result.get("plan_decision") == "reject":
        print(f"Plan was rejected -- nothing was written. ({result.get('rejection_reason', '')})")
        return 0
    if result.get("gave_up"):
        print(f"Gave up after {result.get('attempt', 1)} attempt(s); "
              f"rolled back: {', '.join(result.get('rolled_back_paths', [])) or '(none)'}")
        return 0
    if result.get("commit_result"):
        print(f"commit: {result['commit_result']}")
        print(f"push:   {result.get('push_result')}")
        if result.get("pr_approved") is False:
            print(f"PR:     not opened ({result.get('pr_rejection_reason', 'rejected')})")
        elif result.get("pr_result"):
            print(f"PR:     {result['pr_result']}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
