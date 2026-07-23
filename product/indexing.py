"""Indexing workflow (Phase 10): feeds an IngestSource into a Retriever.
Batched so embedding happens in chunks, not one call per document.
"""
from __future__ import annotations
from core.ingest import IngestSource
from core.retriever import Retriever

def index_repo(ingest: IngestSource, retriever: Retriever, batch_size: int = 16) -> int:
    count = 0
    batch = []
    for doc in ingest.documents():
        batch.append(doc)
        if len(batch) >= batch_size:
            retriever.add(batch)
            count += len(batch)
            batch = []
    if batch:
        retriever.add(batch)
        count += len(batch)
    return count
