"""CLI (Phase 16): index an arbitrary repo path into the retriever.

  python -m app.index [repo_path]

Defaults to forge.repo_path from config.yaml when no path is given. This is
the same pipeline app/ingest_smoke_test.py exercises (product/indexing.py's
index_repo) and the one the Chainlit /index command calls into via
run_index() below -- one indexing pipeline, three ways to trigger it.
"""
from __future__ import annotations
import sys
from typing import Callable
from app.config_loader import load_config
from app.wiring import build_ingest, build_retriever
from core.retriever import Retriever
from product.indexing import index_repo

def run_index(
    repo_path: str,
    cfg: dict,
    retriever: Retriever,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict:
    ingest = build_ingest(cfg, repo_path=repo_path)
    return index_repo(ingest, retriever, on_progress=on_progress)

def main() -> int:
    cfg = load_config()
    repo_path = sys.argv[1] if len(sys.argv) > 1 else cfg["forge"]["repo_path"]
    retriever = build_retriever(cfg)
    stats = run_index(repo_path, cfg, retriever)
    print(f"Indexed {stats['symbols']} symbol(s) across {stats['files']} file(s) from {repo_path}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
