"""Acceptance check for the Qdrant alternative retriever
(adapters/retriever_qdrant.py): proves it satisfies the same Retriever port
contract as ChromaRetriever -- same add()/search() signatures, same
skeleton-index payload (full chunk text + path/start_line/end_line/symbol
metadata), same path-based metadata filtering (product/code_edit.py's
`retriever.search(..., path=target_path)` pattern) -- using embedded/on-disk
Qdrant (no server, no docker needed) so this runs anywhere.

Mirrors app/retriever_smoke_test.py's document set and query so the two are
directly comparable. Also drives it through app/wiring.py's build_retriever()
with retriever.adapter overridden to "qdrant", the exact path production code
takes when someone flips config.yaml -- not just the adapter in isolation.

Run from the repo root:  python -m app.qdrant_retriever_smoke_test
"""
from __future__ import annotations
import copy
import os
import shutil
import sys
from pathlib import Path

from app.config_loader import load_config
from app.wiring import build_retriever
from core.types import Document

SCRATCH = Path(__file__).resolve().parent.parent / ".data" / "qdrant_retriever_smoke_scratch"

def _rmtree_force(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)

def main() -> int:
    _rmtree_force(SCRATCH)
    SCRATCH.mkdir(parents=True)

    cfg = copy.deepcopy(load_config())
    cfg["retriever"]["adapter"] = "qdrant"
    cfg["retriever"]["qdrant_url"] = ""  # force embedded/on-disk mode, no server
    cfg["retriever"]["qdrant_path"] = str(SCRATCH / "qdrant")
    cfg["retriever"]["qdrant_collection"] = "smoke_test"
    cfg["retriever"]["lexical_path"] = str(SCRATCH / "lexical.db")

    try:
        retriever = build_retriever(cfg)
        print(f"[ok] wired {type(retriever).__name__} over {type(retriever._semantic).__name__}")

        if type(retriever._semantic).__name__ != "QdrantRetriever":
            print(f"[!!] expected build_retriever(adapter=qdrant) to use QdrantRetriever, got {type(retriever._semantic).__name__}")
            return 1

        # --- 1. same document set + query as app/retriever_smoke_test.py's Chroma check ---
        docs = [
            Document(content="def add(a, b):\n    return a + b", metadata={"path": "math_utils.py", "symbol": "add", "start_line": 1, "end_line": 2}),
            Document(content="class HttpClient:\n    def get(self, url):\n        ...", metadata={"path": "http_client.py", "symbol": "HttpClient", "start_line": 1, "end_line": 3}),
            Document(content="def parse_yaml(path):\n    return yaml.safe_load(open(path))", metadata={"path": "config_loader.py", "symbol": "parse_yaml", "start_line": 1, "end_line": 2}),
        ]
        retriever.add(docs)
        print(f"[ok] add() -> indexed {len(docs)} document(s)")

        query = "function that adds two numbers together"
        results = retriever.search(query, k=2)
        if not results:
            print("[!!] search() returned no results")
            return 1
        top = results[0]
        print(f"[ok] search({query!r}) -> top hit: {top.metadata.get('path')} (score={top.score:.3f})")
        if top.metadata.get("path") != "math_utils.py":
            print("[!!] expected math_utils.py to rank first for this query -- same result app/retriever_smoke_test.py expects from Chroma")
            return 1
        if top.content != docs[0].content:
            print(f"[!!] expected search() to return the full stored chunk body (skeleton-index contract), got: {top.content!r}")
            return 1
        print("[ok] returned chunk carries the full indexed content, not just a pointer -- matches ChromaRetriever's contract")

        # --- 2. path metadata filter (product/code_edit.py's usage pattern) ---
        filtered = retriever.search(query, k=5, path="http_client.py")
        if any(c.metadata.get("path") != "http_client.py" for c in filtered):
            print(f"[!!] path filter leaked non-matching results: {[c.metadata.get('path') for c in filtered]}")
            return 1
        if not filtered or filtered[0].metadata.get("path") != "http_client.py":
            print("[!!] path filter should still surface the matching document")
            return 1
        print("[ok] path= metadata filter matches product/code_edit.py's `retriever.search(..., path=target_path)` usage")

        print("\nQdrant retriever OK -- same Retriever port, same skeleton-index contract, "
              "same metadata filtering as the default Chroma adapter.")
        return 0
    finally:
        _rmtree_force(SCRATCH)

if __name__ == "__main__":
    sys.exit(main())
