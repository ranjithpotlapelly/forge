"""Phase 10 smoke test: proves the ingest source extracts real structure-aware
chunks from a real repo and that indexing them makes real code semantically
searchable — not the 3 hardcoded docs every earlier smoke test used.

Checks against whatever config.yaml's forge.repo_path currently points at
(the rag-frontend Angular app as of this writing, not Forge's own source --
update the two (query, expected_path) pairs below if repo_path ever changes
again): (1) documents() yields a sane number of real chunks, (2) indexing
them into the retriever and asking about the auth guard surfaces
auth.guard.ts, (3) same for the JWT-attaching interceptor.
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
        ("how does the auth guard block access to a route when the user isn't logged in?", "rag-frontend/src/app/core/guards/auth.guard.ts"),
        ("how is the JWT token attached to outgoing HTTP requests?", "rag-frontend/src/app/core/interceptors/auth.interceptor.ts"),
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
