"""Phase 10 smoke test: proves the ingest source extracts real structure-aware
chunks from this repo and that indexing them makes real code semantically
searchable — not the 3 hardcoded docs every earlier smoke test used.

Checks: (1) documents() yields a sane number of real function/class chunks,
(2) indexing them into the retriever and asking a real question about the
approval gate surfaces product/approval.py, (3) same for the MCP sandbox.
Run from the repo root:  python -m app.ingest_smoke_test
"""
from __future__ import annotations
import sys
from app.config_loader import load_config
from app.wiring import build_ingest, build_retriever
from product.indexing import index_repo

def main() -> int:
    cfg = load_config()
    ingest = build_ingest(cfg)
    print(f"[ok] wired {type(ingest).__name__} (repo_path={ingest.repo_path})")

    docs = list(ingest.documents())
    if len(docs) < 20:
        print(f"[!!] expected a few dozen real chunks, got {len(docs)}")
        return 1
    files_touched = len({d.metadata["path"] for d in docs})
    print(f"[ok] documents() -> {len(docs)} chunk(s) across {files_touched} file(s)")
    sample = docs[0]
    print(f"     e.g. {sample.metadata['path']}:{sample.metadata['symbol']} "
          f"(lines {sample.metadata['start_line']}-{sample.metadata['end_line']})")

    retriever = build_retriever(cfg)
    stats = index_repo(ingest, retriever)
    print(f"[ok] index_repo() -> indexed {stats['symbols']} chunk(s) across {stats['files']} file(s)")

    checks = [
        ("how does the human approval gate for a tool call work?", "product/approval.py"),
        ("how does the MCP workspace server stop a path from escaping the sandbox?", "adapters/mcp_servers/workspace_server.py"),
    ]
    for query, expected_path in checks:
        results = retriever.search(query, k=3)
        if not results:
            print(f"[!!] search({query!r}) returned nothing")
            return 1
        top = results[0]
        print(f"[ok] search({query!r}) -> top hit: {top.metadata.get('path')}:{top.metadata.get('symbol')} (score={top.score:.3f})")
        if top.metadata.get("path") != expected_path:
            print(f"[!!] expected top hit to be {expected_path}, got {top.metadata.get('path')}")
            return 1

    print("\nPhase 10 OK. Real repo code is indexed and semantically searchable.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
