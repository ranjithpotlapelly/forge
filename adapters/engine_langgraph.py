"""Adapter (Phase 4/5): implements core.engine.Engine via a LangGraph
StateGraph, checkpointed to SQLite so graph state survives across process runs.

The graph itself is vendor plumbing; the node logic it calls lives in
product/graph.py and knows nothing about LangGraph.
"""
from __future__ import annotations
import sqlite3
from functools import partial
from pathlib import Path
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import StateGraph, END
from opentelemetry import trace
from core.llm import LLMClient
from core.retriever import Retriever
from core.types import Answer
from product.graph import GraphState, retrieve, has_context, answer, decline

_tracer = trace.get_tracer(__name__)

class LangGraphEngine:
    def __init__(self, llm: LLMClient, retriever: Retriever, k: int = 8, checkpoint_path: str | None = None):
        self.llm, self.retriever, self.k = llm, retriever, k
        self._checkpointer = self._build_checkpointer(checkpoint_path)
        self._graph = self._build()

    def _build_checkpointer(self, checkpoint_path: str | None):
        if not checkpoint_path:
            return None
        Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(checkpoint_path, check_same_thread=False)
        # Graph state carries our own value types (core.types.Chunk/Answer); allowlist
        # them explicitly so checkpointing keeps working once langgraph starts
        # rejecting unregistered types by default.
        serde = JsonPlusSerializer(allowed_msgpack_modules=[("core.types", "Chunk"), ("core.types", "Answer")])
        saver = SqliteSaver(conn, serde=serde)
        saver.setup()
        return saver

    def _build(self):
        g = StateGraph(GraphState)
        g.add_node("retrieve", partial(retrieve, retriever=self.retriever, k=self.k))
        g.add_node("answer", partial(answer, llm=self.llm))
        g.add_node("decline", decline)
        g.set_entry_point("retrieve")
        g.add_conditional_edges("retrieve", has_context, {"answer": "answer", "decline": "decline"})
        g.add_edge("answer", END)
        g.add_edge("decline", END)
        return g.compile(checkpointer=self._checkpointer)

    def ask(self, question: str, thread_id: str = "default") -> Answer:
        with _tracer.start_as_current_span("engine.ask") as span:
            span.set_attribute("engine.thread_id", thread_id)
            span.set_attribute("engine.question", question)
            config = {"configurable": {"thread_id": thread_id}}
            result = self._graph.invoke({"question": question}, config=config)
            answer_ = result["answer"]
            span.set_attribute("engine.answer.citation_count", len(answer_.citations))
            return answer_
