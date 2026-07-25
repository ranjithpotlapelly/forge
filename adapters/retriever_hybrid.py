"""Adapter (Phase 15): implements core.retriever.Retriever by fusing the
lexical SQLite FTS5 index (retriever_fts.py) with a semantic retriever
(retriever_chroma.py) via Reciprocal Rank Fusion.

This is the only piece of the retrieval stack callers ever see -- app/wiring.py
wires it in place of ChromaRetriever, so core.engine and every product/ caller
keep talking to plain Retriever.add()/.search(). Neither sub-index is a
Retriever on its own (FtsIndex.search takes no **filters, ChromaRetriever is
still one directly): this class is what makes the pair conform to the port.
"""
from __future__ import annotations
from core.retriever import Retriever
from core.types import Chunk, Document
from adapters._doc_id import document_id
from adapters.retriever_fts import FtsIndex

# Standard RRF damping constant (Cormack et al.): large enough that a
# document's exact rank within the top few results doesn't swing its fused
# score wildly, small enough that rank 1 still clearly outweighs rank 50.
_RRF_K = 60
# Each sub-search fetches more than the final k so fusion has enough of both
# lists to work with -- a doc ranked #20 lexically but #1 semantically (or
# vice versa) should still be able to surface after fusion.
_CANDIDATE_MULTIPLIER = 5
_MIN_CANDIDATES = 25
# Confidence reported for a chunk the lexical index found that semantic
# search didn't surface at all: an exact match on symbol/signature/path is
# strong, unambiguous evidence of relevance -- worth at least as much as a
# solid cosine-similarity hit, so callers gating on score (has_context, below
# MIN_RELEVANCE) don't quietly ignore it.
_LEXICAL_ONLY_CONFIDENCE = 1.0

def _chunk_id(chunk: Chunk) -> str:
    return document_id(Document(content=chunk.content, metadata=chunk.metadata))

def _matches(metadata: dict, filters: dict) -> bool:
    return all(metadata.get(key) == value for key, value in filters.items())

class HybridRetriever:
    def __init__(self, semantic: Retriever, lexical_path: str):
        self._semantic = semantic
        self._lexical = FtsIndex(lexical_path)

    def add(self, documents: list[Document]) -> None:
        documents = list(documents)
        if not documents:
            return
        self._semantic.add(documents)
        self._lexical.add(documents)

    def search(self, query: str, k: int = 8, **filters) -> list[Chunk]:
        pool = max(k * _CANDIDATE_MULTIPLIER, _MIN_CANDIDATES)
        semantic_hits = self._semantic.search(query, k=pool, **filters)
        lexical_hits = self._lexical.search(query, k=pool)
        if filters:
            lexical_hits = [c for c in lexical_hits if _matches(c.metadata, filters)]

        fused_score: dict[str, float] = {}
        chunks: dict[str, Chunk] = {}
        lexical_rank: dict[str, int] = {}
        # RRF's rank-only fusion decides ORDER well but is meaningless as a
        # confidence VALUE: a chunk that's merely the #1 semantic hit for a
        # totally unrelated query gets the same fused score as a genuinely
        # strong match, since only rank position feeds the formula, not the
        # underlying similarity magnitude. Callers like has_context() need a
        # real confidence number, so report the original semantic score
        # (the one MIN_RELEVANCE is actually calibrated against) instead of
        # the fused one -- fused_score still decides sort order, just not
        # what's returned as Chunk.score.
        confidence: dict[str, float] = {}
        for rank, chunk in enumerate(semantic_hits, start=1):
            cid = _chunk_id(chunk)
            fused_score[cid] = fused_score.get(cid, 0.0) + 1.0 / (_RRF_K + rank)
            chunks.setdefault(cid, chunk)
            confidence[cid] = chunk.score if chunk.score is not None else 0.0
        for rank, chunk in enumerate(lexical_hits, start=1):
            cid = _chunk_id(chunk)
            fused_score[cid] = fused_score.get(cid, 0.0) + 1.0 / (_RRF_K + rank)
            chunks.setdefault(cid, chunk)
            lexical_rank[cid] = rank
            confidence.setdefault(cid, _LEXICAL_ONLY_CONFIDENCE)

        # RRF ties exactly whenever two documents simply swap rank between
        # the two lists (e.g. each is #1 in one and #2 in the other -- a
        # subclass that *mentions* a class can out-embed the class's own
        # definition). Break ties toward the better lexical rank: an exact
        # match on symbol/signature/path is a precise signal where cosine
        # similarity is a fuzzy one, and for an identifier-shaped query that
        # precision should decide it.
        def sort_key(cid: str) -> tuple:
            return (-fused_score[cid], lexical_rank.get(cid, float("inf")))

        ranked = sorted(fused_score, key=sort_key)[:k]
        return [Chunk(content=chunks[cid].content, metadata=chunks[cid].metadata, score=confidence[cid]) for cid in ranked]
