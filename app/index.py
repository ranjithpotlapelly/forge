"""CLI (Phase 16): index an arbitrary repo path into the retriever.

  python -m app.index [repo_path] [--changed]

Defaults to forge.repo_path from config.yaml when no path is given. This is
the same pipeline app/ingest_smoke_test.py exercises (product/indexing.py's
index_repo) and the one the Chainlit /index command calls into via
run_index() below -- one indexing pipeline, three ways to trigger it.

--changed switches to run_incremental_index(): only the working tree's
uncommitted changes (vs HEAD) get re-embedded, via
adapters.ingest_fs.changed_paths() + product.indexing.index_changed_files.
Much faster than a full walk once a repo's been indexed once; use it right
before committing so the index reflects what you're about to commit without
re-walking everything else.
"""
from __future__ import annotations
import sys
from typing import Callable
from adapters.ingest_fs import changed_paths
from app.config_loader import load_config
from app.wiring import build_ingest, build_retriever
from core.retriever import Retriever
from product.indexing import index_changed_files, index_repo

def run_index(
    repo_path: str,
    cfg: dict,
    retriever: Retriever,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict:
    ingest = build_ingest(cfg, repo_path=repo_path)
    return index_repo(ingest, retriever, on_progress=on_progress)

def run_incremental_index(
    repo_path: str,
    cfg: dict,
    retriever: Retriever,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Reindex only what's changed in repo_path's working tree since HEAD --
    see adapters.ingest_fs.changed_paths() for exactly what counts as
    'changed'. Raises if repo_path isn't a git repo (changed_paths() shells
    out to `git diff`/`git ls-files`)."""
    ingest = build_ingest(cfg, repo_path=repo_path)
    changed, deleted = changed_paths(repo_path)
    return index_changed_files(ingest, retriever, changed, deleted, on_progress=on_progress)

def main() -> int:
    cfg = load_config()
    args = [a for a in sys.argv[1:] if a != "--changed"]
    incremental = "--changed" in sys.argv[1:]
    repo_path = args[0] if args else cfg["forge"]["repo_path"]
    retriever = build_retriever(cfg)
    if incremental:
        stats = run_incremental_index(repo_path, cfg, retriever)
        print(f"Reindexed {stats['symbols']} symbol(s) across {stats['files']} changed file(s) "
              f"({stats['deleted']} deleted) from {repo_path}")
    else:
        stats = run_index(repo_path, cfg, retriever)
        print(f"Indexed {stats['symbols']} symbol(s) across {stats['files']} file(s) from {repo_path}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
