"""Forge's question-answering decision graph: node logic only. LangGraph wires
these functions together in adapters/engine_langgraph.py — nothing here
imports LangGraph or any other vendor.
"""
from __future__ import annotations
from typing import TypedDict
from core.llm import LLMClient
from core.retriever import Retriever
from core.types import Answer, Chunk, Message

MIN_RELEVANCE = 0.2  # below this, treat retrieval as "no relevant context"

class GraphState(TypedDict, total=False):
    question: str
    chunks: list[Chunk]
    answer: Answer

def retrieve(state: GraphState, retriever: Retriever, k: int = 8) -> GraphState:
    return {"chunks": retriever.search(state["question"], k=k)}

def has_context(state: GraphState) -> str:
    """Decision: route to 'answer' if retrieval found relevant chunks, else 'decline'."""
    chunks = state.get("chunks") or []
    if chunks and chunks[0].score is not None and chunks[0].score >= MIN_RELEVANCE:
        return "answer"
    return "decline"

def answer(state: GraphState, llm: LLMClient) -> GraphState:
    chunks = state["chunks"]
    context = "\n\n".join(
        f"[{c.metadata.get('path', '?')}:{c.metadata.get('start_line', '?')}]\n{c.content}"
        for c in chunks
    )
    messages = [
        Message(role="system", content=(
            "Answer the question using only the provided code context. "
            "Cite sources inline as [path:line]."
        )),
        Message(role="user", content=f"Context:\n{context}\n\nQuestion: {state['question']}"),
    ]
    return {"answer": Answer(text=llm.generate(messages), citations=chunks)}

def decline(state: GraphState) -> GraphState:
    return {"answer": Answer(text="I don't have enough indexed context to answer that.", citations=[])}
