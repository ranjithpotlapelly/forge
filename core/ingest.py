"""Port: a source of knowledge to index.

Forge's adapter (Phase 3) walks a code repo and yields structure-aware chunks
(by function/class) with path + line metadata — but the engine only ever sees
this interface, so a different source (docs, tickets) is a drop-in replacement.
"""
from __future__ import annotations
from typing import Protocol, Iterable, runtime_checkable
from core.types import Document

@runtime_checkable
class IngestSource(Protocol):
    def documents(self) -> Iterable[Document]:
        """Yield documents to be indexed by a Retriever."""
        ...
