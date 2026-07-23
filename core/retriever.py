"""Port: knowledge retrieval. Adapters: Chroma (local) -> Qdrant (upgrade)."""
from __future__ import annotations
from typing import Protocol, runtime_checkable
from core.types import Document, Chunk

@runtime_checkable
class Retriever(Protocol):
    def add(self, documents: list[Document]) -> None:
        """Index documents (embedding handled inside the adapter)."""
        ...

    def search(self, query: str, k: int = 8, **filters) -> list[Chunk]:
        """Return the top-k most relevant chunks, optionally metadata-filtered."""
        ...
