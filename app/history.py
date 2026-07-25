"""CLI (Phase 19): list recent runs, or show one run's steps.

  python -m app.history                 # list recent runs
  python -m app.history <run_id>        # show that run's steps (accepts a prefix, like a short git hash)

Reads via core.run_history.RunHistory (adapters/run_history_sqlite.py),
populated automatically by every Q&A/task run through LangGraphEngine and by
the Chainlit Q&A streaming path (app/chainlit_app.py) -- nothing here writes
history, it only reads what those paths already recorded.
"""
from __future__ import annotations
import json
import sys
from app.config_loader import load_config
from app.wiring import build_run_history

def _truncate(text: str, n: int = 50) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"

def _short_ts(ts: str | None) -> str:
    """ISO timestamps carry microseconds ("...T14:16:24.888473+00:00"),
    more precision than a listing needs -- trim to whole seconds."""
    return (ts or "")[:19].replace("T", " ")

def _list_runs(history) -> int:
    runs = history.list_runs(limit=20)
    if not runs:
        print("No runs recorded yet. Ask a question or run a task first.")
        return 0
    print(f"{'id':<10}{'kind':<7}{'status':<11}{'started_at':<21}task_text")
    for r in runs:
        print(f"{r['id'][:8]:<10}{r['kind']:<7}{r['status']:<11}{_short_ts(r['started_at']):<21}{_truncate(r['task_text'])}")
    print(f"\n{len(runs)} run(s) shown (most recent first). Inspect one: "
          f"python -m app.history <id, or just its first 8 chars>")
    return 0

def _show_run(history, run_id: str) -> int:
    run = history.get_run(run_id)
    if run is None:
        print(f"No run found matching {run_id!r}")
        return 1

    print(f"Run {run['id']}")
    print(f"  thread_id:  {run['thread_id']}")
    print(f"  kind:       {run['kind']}")
    print(f"  task_text:  {run['task_text']}")
    print(f"  status:     {run['status']}")
    print(f"  started_at: {_short_ts(run['started_at'])}")
    print(f"  ended_at:   {_short_ts(run['ended_at']) or '(still running)'}")
    if run["error"]:
        print(f"  error:      {run['error']}")

    steps = history.list_steps(run["id"])
    print(f"\n  {len(steps)} step(s):")
    if not steps:
        print("  (none recorded)")
        return 0

    for s in steps:
        duration = f"{s['duration_ms']:.0f}ms" if s["duration_ms"] is not None else "-"
        print(f"  [{s['id']}] {s['node']:<16} {s['status']:<7} {duration:<9} {_short_ts(s['created_at'])}")
        if not s["detail"]:
            continue
        try:
            parsed = json.loads(s["detail"])
        except (TypeError, ValueError):
            print(f"        {s['detail']}")
            continue
        if "decision" in parsed:  # an approval step: print the audit trail clearly
            print(f"        decision: {parsed['decision']}")
            if parsed.get("feedback"):
                print(f"        feedback: {parsed['feedback']}")
            print("        proposed:")
            for line in json.dumps(parsed["proposed"], indent=2).splitlines():
                print(f"          {line}")
        else:
            print(f"        {json.dumps(parsed)}")
    return 0

def main() -> int:
    cfg = load_config()
    history = build_run_history(cfg)
    if len(sys.argv) > 1:
        return _show_run(history, sys.argv[1])
    return _list_runs(history)

if __name__ == "__main__":
    sys.exit(main())
