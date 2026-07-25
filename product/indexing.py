"""Indexing workflow (Phase 10): feeds an IngestSource into a Retriever.
Batched so embedding happens in chunks, not one call per document.
"""
from __future__ import annotations
from typing import Callable
from core.ingest import IngestSource
from core.retriever import Retriever
from core.types import Document

def index_repo(
    ingest: IngestSource,
    retriever: Retriever,
    batch_size: int = 16,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Returns {"files": unique file count, "symbols": chunk count}.

    on_progress(symbols_so_far, files_so_far), if given, fires after every
    batch is added to the retriever -- e.g. a Chainlit /index command uses it
    to show a live counter instead of one silent, possibly slow, blocking call.
    """
    files: set[str] = set()
    symbols = 0
    batch: list[Document] = []
    for doc in ingest.documents():
        files.add(doc.metadata.get("path", ""))
        symbols += 1
        batch.append(doc)
        if len(batch) >= batch_size:
            retriever.add(batch)
            batch = []
            if on_progress:
                on_progress(symbols, len(files))
    if batch:
        retriever.add(batch)
    if on_progress:
        on_progress(symbols, len(files))
    return {"files": len(files), "symbols": symbols}
