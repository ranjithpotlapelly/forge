"""Shared helper (Phase 15): a stable per-Document id, used by every
retriever adapter so results from independent indexes (Chroma, the FTS5
lexical index) can be recognized as "the same chunk" and fused by identity.

Not a port -- both retriever_chroma.py and retriever_fts.py import this
directly. It has lived on ChromaRetriever alone since Phase 3; extracted here
unchanged (same hash) so existing Chroma collections keep the same ids.
"""
from __future__ import annotations
import hashlib
from core.types import Document

def document_id(doc: Document) -> str:
    meta = doc.metadata or {}
    if "path" in meta:
        key = f"{meta['path']}:{meta.get('start_line', '')}:{meta.get('symbol', '')}"
    else:
        key = doc.content
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
