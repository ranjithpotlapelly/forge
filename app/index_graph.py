"""CLI (Prompt 27, additive): build the dependency graph for a repo.

  python -m app.index_graph [repo_path] [--clear]

Defaults to forge.repo_path from config.yaml when no path is given. Deliberately
separate from `python -m app.index` -- this never runs as a side effect of the
normal indexing path, so leaving retriever.graph_expand at its default (off)
means this command simply never gets invoked and nothing about /index changes.

Scoped to Java + TypeScript (adapters/code_graph_extract.py's _EXTENSIONS) --
run this again after `python -m app.index --changed` if you want the graph
to track further edits; there is no incremental mode yet, only a full rebuild.
"""
from __future__ import annotations
import sys
from adapters.code_graph_extract import extract_repo
from app.config_loader import load_config
from app.wiring import build_code_graph_store
from product.code_graph import build_graph

def main() -> int:
    cfg = load_config()
    args = [a for a in sys.argv[1:] if a != "--clear"]
    clear = "--clear" in sys.argv[1:]
    repo_path = args[0] if args else cfg["forge"]["repo_path"]

    graph_store = build_code_graph_store(cfg)
    if clear:
        graph_store.clear()
        print("Cleared the dependency graph.")

    stats = build_graph(extract_repo, graph_store, repo_path)
    print(f"Built dependency graph: {stats['nodes']} node(s), {stats['edges']} edge(s) from {repo_path}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
