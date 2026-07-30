"""Adapter (alternative to retriever_chroma.py, additive-only): implements
core.retriever.Retriever via Qdrant, as a sibling file proving the Retriever
port can swap vector stores without touching core/, product/, or app/ask.py.

Same skeleton-index contract as ChromaRetriever: the payload holds the full
chunk text plus path/start_line/end_line/symbol metadata (mirroring how
ChromaRetriever's upsert stores documents=contents) -- search() returns that
stored text directly as Chunk.content, no re-read from disk. Same embedder
injection too: embeddings are computed through Ollama (same embed_model/host
config as Chroma), so this adapter has no vendor coupling beyond
qdrant-client + ollama.

Local Qdrant only, two ways (see config.yaml's retriever.qdrant_url/
qdrant_path -- url wins if both are set):
- a running Qdrant server (e.g. `docker run -p 6333:6333 qdrant/qdrant`)
- embedded/on-disk mode (QdrantClient(path=...), no server needed -- same
  idea as ChromaRetriever's PersistentClient)

retriever.adapter stays "chroma" by default (app/wiring.py); this only
activates when config.yaml explicitly sets retriever.adapter: qdrant.
"""
from __future__ import annotations
import uuid
import ollama
from opentelemetry import trace
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from adapters._doc_id import document_id
from core.types import Document, Chunk

_tracer = trace.get_tracer(__name__)

class QdrantRetriever:
    def __init__(self, collection: str, embed_model: str, host: str, url: str | None = None, path: str | None = None, **opts):
        if not url and not path:
            raise ValueError("QdrantRetriever needs retriever.qdrant_url (server) or retriever.qdrant_path (embedded/on-disk)")
        self.collection = collection
        self.embed_model, self.host = embed_model, host
        self._ollama = ollama.Client(host=host)
        self._client = QdrantClient(url=url) if url else QdrantClient(path=path)
        self._ensured = False

    def _ensure_collection(self, vector_size: int) -> None:
        if self._ensured:
            return
        if not self._client.collection_exists(self.collection):
            self._client.create_collection(
                collection_name=self.collection,
                vectors_config=qmodels.VectorParams(size=vector_size, distance=qmodels.Distance.COSINE),
            )
        self._ensured = True

    def _embed(self, texts: list[str]) -> list[list[float]]:
        with _tracer.start_as_current_span("retriever.embed") as span:
            span.set_attribute("embedding.model", self.embed_model)
            span.set_attribute("embedding.batch_size", len(texts))
            return self._ollama.embed(model=self.embed_model, input=texts).embeddings

    @staticmethod
    def _point_id(doc: Document) -> str:
        # Qdrant point ids must be an unsigned int or a UUID string -- reinterpret
        # document_id()'s sha256 hex digest as a UUID (first 32 hex chars = 16
        # bytes) rather than inventing a second id scheme, so re-indexing the
        # same chunk (same path/start_line/symbol) still upserts the same point.
        return str(uuid.UUID(document_id(doc)[:32]))

    def add(self, documents: list[Document]) -> None:
        if not documents:
            return
        with _tracer.start_as_current_span("retriever.add") as span:
            span.set_attribute("retriever.collection", self.collection)
            span.set_attribute("retriever.doc_count", len(documents))
            contents = [d.content for d in documents]
            embeddings = self._embed(contents)
            self._ensure_collection(len(embeddings[0]))
            points = [
                qmodels.PointStruct(
                    id=self._point_id(doc),
                    vector=vector,
                    payload={"content": doc.content, **(doc.metadata or {})},
                )
                for doc, vector in zip(documents, embeddings)
            ]
            self._client.upsert(collection_name=self.collection, points=points)

    def search(self, query: str, k: int = 8, **filters) -> list[Chunk]:
        with _tracer.start_as_current_span("retriever.search") as span:
            span.set_attribute("retriever.collection", self.collection)
            span.set_attribute("retriever.query", query)
            span.set_attribute("retriever.k", k)
            query_embedding = self._embed([query])[0]
            if not self._client.collection_exists(self.collection):
                return []
            results = self._client.query_points(
                collection_name=self.collection,
                query=query_embedding,
                limit=k,
                query_filter=self._build_filter(filters),
            ).points
            chunks = []
            for point in results:
                payload = dict(point.payload or {})
                content = payload.pop("content", "")
                chunks.append(Chunk(content=content, metadata=payload, score=point.score))
            span.set_attribute("retriever.result_count", len(chunks))
            return chunks

    @staticmethod
    def _build_filter(filters: dict) -> qmodels.Filter | None:
        if not filters:
            return None
        return qmodels.Filter(must=[
            qmodels.FieldCondition(key=key, match=qmodels.MatchValue(value=value))
            for key, value in filters.items()
        ])
