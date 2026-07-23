"""Composition root: builds adapters from config. The only place that maps a
config['*']['adapter'] name to a concrete class.
"""
from __future__ import annotations
import sys
from core.engine import Engine
from core.llm import LLMClient
from core.observability import Tracing
from core.retriever import Retriever
from core.store import StateStore
from core.tools import Tool

def build_llm(cfg: dict) -> LLMClient:
    section = cfg["llm"]
    adapter = section["adapter"]
    if adapter == "ollama":
        from adapters.llm_ollama import OllamaLLM
        rest = {k: v for k, v in section.items() if k not in ("adapter", "model", "host")}
        return OllamaLLM(model=section["model"], host=section["host"], **rest)
    raise ValueError(f"Unknown llm adapter: {adapter}")

def build_retriever(cfg: dict) -> Retriever:
    section = cfg["retriever"]
    adapter = section["adapter"]
    if adapter == "chroma":
        from adapters.retriever_chroma import ChromaRetriever
        return ChromaRetriever(
            path=section["path"],
            collection=section["collection"],
            embed_model=cfg["embeddings"]["model"],
            host=cfg["embeddings"].get("host", cfg["llm"]["host"]),
        )
    raise ValueError(f"Unknown retriever adapter: {adapter}")

def build_engine(cfg: dict, llm: LLMClient | None = None, retriever: Retriever | None = None) -> Engine:
    section = cfg["engine"]
    adapter = section["adapter"]
    if adapter == "langgraph":
        from adapters.engine_langgraph import LangGraphEngine
        return LangGraphEngine(
            llm=llm or build_llm(cfg),
            retriever=retriever or build_retriever(cfg),
            k=cfg["retriever"].get("top_k", 8),
            checkpoint_path=section.get("checkpoint_path"),
        )
    raise ValueError(f"Unknown engine adapter: {adapter}")

def build_store(cfg: dict) -> StateStore:
    section = cfg["store"]
    adapter = section["adapter"]
    if adapter == "sqlite":
        from adapters.store_sqlite import SqliteStore
        return SqliteStore(path=section["path"])
    raise ValueError(f"Unknown store adapter: {adapter}")

def build_tracing(cfg: dict) -> Tracing:
    section = cfg["observability"]
    adapter = section["adapter"]
    if adapter == "phoenix":
        from adapters.observability_phoenix import PhoenixTracing
        return PhoenixTracing(
            endpoint=section["endpoint"],
            project_name=section.get("project_name", "forge"),
            enabled=section.get("enabled", True),
        )
    raise ValueError(f"Unknown observability adapter: {adapter}")

def build_tools(cfg: dict) -> list[Tool]:
    section = cfg["tools"]
    adapter = section["adapter"]
    if adapter == "mcp":
        from adapters.tools_mcp import load_mcp_tools
        return load_mcp_tools(
            command=sys.executable,
            args=[section["server_script"], section["workspace"]],
            require_approval_for=set(cfg["forge"]["require_approval_for"]),
        )
    raise ValueError(f"Unknown tools adapter: {adapter}")
