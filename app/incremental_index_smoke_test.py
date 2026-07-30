"""Smoke test: proves changed_paths()/index_changed_files() (the --changed
path added to app/index.py) only touch files that actually changed in the
working tree, and that Retriever.delete() (Chroma + the FTS5 lexical index)
correctly purges a deleted file's chunks instead of leaving them searchable
forever.

Checks: (1) an initial index_repo() over a throwaway git repo, (2) after
modifying one file, adding a new one, and deleting a third, changed_paths()
reports exactly those three, (3) index_changed_files() re-embeds only the
modified+added files and removes the deleted one's chunks -- untouched files'
entries are left alone, (4) FtsIndex.delete() (used by HybridRetriever)
removes the right row even though path isn't stored verbatim in its FTS
table.
Run from the repo root:  python -m app.incremental_index_smoke_test
"""
from __future__ import annotations
import shutil
import subprocess
import sys
from pathlib import Path
from adapters.ingest_fs import FsIngestSource, changed_paths
from adapters.retriever_chroma import ChromaRetriever
from adapters.retriever_fts import FtsIndex
from app.config_loader import load_config
from core.types import Document
from product.indexing import index_changed_files, index_repo

SCRATCH = Path("./.data/incremental_index_smoke_test").resolve()

def _git(*args: str) -> None:
    result = subprocess.run(["git", *args], cwd=SCRATCH, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")

def _write(rel: str, content: str) -> None:
    path = SCRATCH / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

def main() -> int:
    cfg = load_config()

    # --- 0. FtsIndex.delete() in isolation, no Ollama/embeddings needed ----
    fts_path = SCRATCH.parent / "incremental_index_smoke_test_fts.db"
    fts_path.unlink(missing_ok=True)
    fts = FtsIndex(str(fts_path))
    keep = Document(content="def keep(): pass", metadata={"path": "keep.py", "symbol": "keep", "start_line": 1})
    drop = Document(content="def drop(): pass", metadata={"path": "drop.py", "symbol": "drop", "start_line": 1})
    fts.add([keep, drop])
    fts.delete(["drop.py"])
    remaining = {c.metadata["path"] for c in fts.search("keep OR drop", k=10)}
    if remaining != {"keep.py"}:
        print(f"[!!] FtsIndex.delete() left {remaining!r}, expected only {{'keep.py'}}")
        return 1
    print("[ok] FtsIndex.delete() removes the right row despite path not being stored verbatim")

    # --- 1. throwaway git repo, three files, real index_repo() -------------
    shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir(parents=True)
    _git("init", "-b", "main")
    _git("config", "user.email", "smoke@localhost")
    _git("config", "user.name", "Smoke Test")
    _write("a.py", "def a():\n    return 1\n")
    _write("b.py", "def b():\n    return 2\n")
    _write("c.py", "def c():\n    return 3\n")
    _git("add", "-A")
    _git("commit", "-m", "initial")

    ingest = FsIngestSource(str(SCRATCH), languages=["python"])
    retriever = ChromaRetriever(
        path=cfg["retriever"]["path"],
        collection="incremental_index_smoke_test",
        embed_model=cfg["embeddings"]["model"],
        host=cfg["embeddings"].get("host", cfg["llm"]["host"]),
    )
    try:  # idempotent re-run after an earlier failed attempt left the collection behind
        retriever._client.delete_collection("incremental_index_smoke_test")
    except Exception:
        pass
    retriever._col = retriever._client.get_or_create_collection(name="incremental_index_smoke_test")
    stats = index_repo(ingest, retriever)
    if stats["files"] != 3:
        print(f"[!!] expected 3 files indexed, got {stats}")
        return 1
    print(f"[ok] initial index_repo() -> {stats}")

    # --- 2. modify a.py, add d.py (untracked), delete c.py ------------------
    _write("a.py", "def a():\n    return 100  # changed\n")
    _write("d.py", "def d():\n    return 4\n")
    (SCRATCH / "c.py").unlink()

    changed, deleted = changed_paths(str(SCRATCH))
    if sorted(changed) != ["a.py", "d.py"] or deleted != ["c.py"]:
        print(f"[!!] expected changed=['a.py','d.py'] deleted=['c.py'], got changed={changed} deleted={deleted}")
        return 1
    print(f"[ok] changed_paths() -> changed={changed}, deleted={deleted}")

    # --- 3. index_changed_files() re-embeds only those, purges c.py --------
    inc_stats = index_changed_files(ingest, retriever, changed, deleted)
    if inc_stats != {"files": 2, "symbols": 2, "deleted": 1}:
        print(f"[!!] expected {{'files': 2, 'symbols': 2, 'deleted': 1}}, got {inc_stats}")
        return 1

    all_docs = retriever._col.get(include=["metadatas", "documents"])
    by_path = {m["path"]: doc for m, doc in zip(all_docs["metadatas"], all_docs["documents"]) if m.get("path") in ("a.py", "b.py", "c.py", "d.py")}
    if "c.py" in by_path:
        print("[!!] c.py was deleted from the working tree but its chunk is still in the retriever")
        return 1
    if "d.py" not in by_path:
        print("[!!] d.py is new but never got indexed")
        return 1
    if "100" not in by_path.get("a.py", ""):
        print(f"[!!] a.py's indexed content wasn't updated: {by_path.get('a.py')!r}")
        return 1
    if "return 2" not in by_path.get("b.py", ""):
        print("[!!] untouched b.py's chunk was unexpectedly changed or removed")
        return 1
    print(f"[ok] index_changed_files() -> {inc_stats}; a.py updated, d.py added, c.py purged, b.py untouched")

    # --- 4. clear() wipes everything, semantic + lexical -------------------
    fts.add([keep])  # re-add so clear() has something real to remove
    retriever.clear()
    fts.clear()
    if retriever._col.count() != 0:
        print(f"[!!] retriever.clear() left {retriever._col.count()} chunk(s) behind")
        return 1
    if fts.search("keep", k=10):
        print("[!!] FtsIndex.clear() left rows behind")
        return 1
    print("[ok] clear() wipes both the semantic collection and the lexical index")

    retriever._client.delete_collection("incremental_index_smoke_test")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    fts_path.unlink(missing_ok=True)

    print("\nIncremental indexing OK: changed_paths() + index_changed_files() only touch what changed.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
