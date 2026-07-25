"""Phase 4 smoke test: proves the LangGraph decision graph actually branches.

Checks: (1) a question with relevant indexed context routes to 'answer' and
returns cited text, (2) the routing decision itself declines when retrieval
finds nothing relevant. Run from the repo root:  python -m app.engine_smoke_test
"""
from __future__ import annotations
import sys
from app.config_loader import load_config
from app.wiring import build_engine
from core.types import Chunk
from adapters.engine_langgraph import has_context

def main() -> int:
    cfg = load_config()
    engine = build_engine(cfg)
    print(f"[ok] wired {type(engine).__name__}")

    # Branch 1: relevant context should be present (indexed by the Phase 3 smoke test).
    question = "What function adds two numbers together?"
    try:
        result = engine.ask(question)
    except Exception as e:  # noqa: BLE001
        print(f"[!!] ask() failed: {e}")
        return 1

    if not result.text.strip():
        print("[!!] ask() returned an empty answer")
        return 1
    print(f"[ok] ask({question!r}) -> {result.text.strip()!r}")

    if not result.citations:
        print("[!!] expected citations for a question with indexed context")
        return 1
    print(f"[ok] cited {len(result.citations)} chunk(s), top = {result.citations[0].metadata.get('path')}")

    # Branch 2: the decline route, exercised directly against the pure routing
    # function so this doesn't depend on finding a query with zero real hits.
    empty_route = has_context({"chunks": []})
    low_score_route = has_context({"chunks": [Chunk(content="x", score=0.01)]})
    if empty_route != "decline" or low_score_route != "decline":
        print(f"[!!] expected 'decline' for empty/low-score chunks, got {empty_route!r}/{low_score_route!r}")
        return 1
    print("[ok] has_context() routes to 'decline' when nothing relevant is retrieved")

    print("\nPhase 4 OK. Next: Phase 5 - State (SQLite checkpointer + store).")
    return 0

if __name__ == "__main__":
    sys.exit(main())
