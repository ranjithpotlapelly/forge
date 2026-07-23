"""Phase 3 smoke test: proves the Chroma retriever actually indexes and searches.

Checks: (1) Retriever.add embeds and persists documents, (2) Retriever.search
returns the most relevant one first. Run from the repo root:
  python -m app.retriever_smoke_test
"""
from __future__ import annotations
import sys
from app.config_loader import load_config
from app.wiring import build_retriever
from core.types import Document

def main() -> int:
    cfg = load_config()
    retriever = build_retriever(cfg)
    print(f"[ok] wired {type(retriever).__name__} (path={retriever.path}, collection={retriever.collection})")

    docs = [
        Document(content="def add(a, b):\n    return a + b", metadata={"path": "math_utils.py", "symbol": "add", "start_line": 1}),
        Document(content="class HttpClient:\n    def get(self, url):\n        ...", metadata={"path": "http_client.py", "symbol": "HttpClient", "start_line": 1}),
        Document(content="def parse_yaml(path):\n    return yaml.safe_load(open(path))", metadata={"path": "config_loader.py", "symbol": "parse_yaml", "start_line": 1}),
    ]

    try:
        retriever.add(docs)
    except Exception as e:  # noqa: BLE001
        print(f"[!!] add() failed: {e}")
        return 1
    print(f"[ok] add() -> indexed {len(docs)} document(s)")

    query = "function that adds two numbers together"
    try:
        results = retriever.search(query, k=2)
    except Exception as e:  # noqa: BLE001
        print(f"[!!] search() failed: {e}")
        return 1

    if not results:
        print("[!!] search() returned no results")
        return 1

    top = results[0]
    print(f"[ok] search({query!r}) -> top hit: {top.metadata.get('path')} (score={top.score:.3f})")
    for c in results:
        print(f"     - {c.metadata.get('path')} score={c.score:.3f}")

    if top.metadata.get("path") != "math_utils.py":
        print("[!!] expected math_utils.py to rank first for this query")
        return 1

    print("\nPhase 3 OK. Next: Phase 4 - Orchestration (LangGraph decision graph).")
    return 0

if __name__ == "__main__":
    sys.exit(main())
