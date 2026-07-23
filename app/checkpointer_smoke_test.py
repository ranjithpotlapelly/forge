"""Phase 5 smoke test: proves the LangGraph checkpointer actually persists
per-thread graph state to SQLite.

Checks: (1) after ask()-ing on a thread, that thread has a checkpoint with the
answer in it, (2) a second, independent engine instance pointed at the same
checkpoint file still sees it (durability, not just in-process memory),
(3) a thread that was never asked has no checkpoint (thread isolation).
Run from the repo root:  python -m app.checkpointer_smoke_test
"""
from __future__ import annotations
import sys
from app.config_loader import load_config
from app.wiring import build_engine

def main() -> int:
    cfg = load_config()
    checkpoint_path = cfg["engine"].get("checkpoint_path")
    if not checkpoint_path:
        print("[!!] config.yaml engine.checkpoint_path is not set")
        return 1

    engine = build_engine(cfg)
    print(f"[ok] wired {type(engine).__name__} with checkpoint_path={checkpoint_path}")

    thread_id = "smoke-thread"
    result = engine.ask("What function adds two numbers together?", thread_id=thread_id)
    if not result.text.strip():
        print("[!!] ask() returned an empty answer")
        return 1
    print(f"[ok] ask(thread_id={thread_id!r}) -> {result.text.strip()!r}")

    config = {"configurable": {"thread_id": thread_id}}
    snapshot = engine._graph.get_state(config)
    if not snapshot.values.get("answer"):
        print("[!!] checkpoint for the used thread has no answer in it")
        return 1
    print(f"[ok] checkpoint for {thread_id!r} holds the answer")

    # A second, independent engine instance against the same file must see it too.
    reopened = build_engine(cfg)
    reopened_snapshot = reopened._graph.get_state(config)
    if reopened_snapshot.values.get("answer") != snapshot.values.get("answer"):
        print("[!!] a fresh engine instance did not see the persisted checkpoint")
        return 1
    print("[ok] checkpoint survives a fresh engine instance against the same file")

    # A thread that was never touched should have no checkpoint.
    untouched = engine._graph.get_state({"configurable": {"thread_id": "never-asked"}})
    if untouched.values:
        print(f"[!!] expected no checkpoint for an untouched thread, got {untouched.values!r}")
        return 1
    print("[ok] untouched thread has no checkpoint (threads are isolated)")

    print("\nPhase 5 OK (checkpointer + store both wired).")
    return 0

if __name__ == "__main__":
    sys.exit(main())
