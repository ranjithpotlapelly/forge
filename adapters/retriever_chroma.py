"""Adapter (Phase 3): implements core.retriever.Retriever via embedded ChromaDB.

Embeddings are computed through Ollama (same server as the LLM, different
model) so this adapter has no vendor coupling beyond chromadb + ollama.
"""
from __future__ import annotations
import chromadb
import ollama
from opentelemetry import trace
from adapters._doc_id import document_id
from core.types import Document, Chunk

_tracer = trace.get_tracer(__name__)

class ChromaRetriever:
    def __init__(self, path: str, collection: str, embed_model: str, host: str, **opts):
        self.path, self.collection = path, collection
        self.embed_model, self.host = embed_model, host
        self._ollama = ollama.Client(host=host)
        self._client = chromadb.PersistentClient(path=path)
        self._col = self._client.get_or_create_collection(name=collection)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        with _tracer.start_as_current_span("retriever.embed") as span:
            span.set_attribute("embedding.model", self.embed_model)
            span.set_attribute("embedding.batch_size", len(texts))
            return self._ollama.embed(model=self.embed_model, input=texts).embeddings

    def add(self, documents: list[Document]) -> None:
        if not documents:
            return
        with _tracer.start_as_current_span("retriever.add") as span:
            span.set_attribute("retriever.collection", self.collection)
            span.set_attribute("retriever.doc_count", len(documents))
            ids = [document_id(d) for d in documents]
            contents = [d.content for d in documents]
            metadatas = [d.metadata or {"_empty": True} for d in documents]
            embeddings = self._embed(contents)
            self._col.upsert(ids=ids, documents=contents, metadatas=metadatas, embeddings=embeddings)

    def delete(self, paths: list[str]) -> None:
        if not paths:
            return
        with _tracer.start_as_current_span("retriever.delete") as span:
            span.set_attribute("retriever.collection", self.collection)
            span.set_attribute("retriever.delete.path_count", len(paths))
            self._col.delete(where={"path": {"$in": paths}})

    def clear(self) -> None:
        with _tracer.start_as_current_span("retriever.clear") as span:
            span.set_attribute("retriever.collection", self.collection)
            self._client.delete_collection(self.collection)
            self._col = self._client.get_or_create_collection(name=self.collection)

    def search(self, query: str, k: int = 8, **filters) -> list[Chunk]:
        with _tracer.start_as_current_span("retriever.search") as span:
            span.set_attribute("retriever.collection", self.collection)
            span.set_attribute("retriever.query", query)
            span.set_attribute("retriever.k", k)
            query_embedding = self._embed([query])[0]
            results = self._col.query(
                query_embeddings=[query_embedding],
                n_results=k,
                where=filters or None,
            )
            chunks = []
            docs = results["documents"][0]
            metas = results["metadatas"][0]
            distances = results["distances"][0]
            for content, metadata, distance in zip(docs, metas, distances):
                chunks.append(Chunk(content=content, metadata=metadata, score=1.0 - distance))
            span.set_attribute("retriever.result_count", len(chunks))
            return chunks
