"""Composition root: builds adapters from config. The only place that maps a
config['*']['adapter'] name to a concrete class.
"""
from __future__ import annotations
import sys
from core.engine import Engine
from core.ingest import IngestSource
from core.llm import LLMClient
from core.observability import Tracing
from core.retriever import Retriever
from core.store import StateStore
from core.tools import Tool

def _build_ollama_llm(section: dict, cfg: dict) -> LLMClient:
    from adapters.llm_ollama import OllamaLLM
    rest = {k: v for k, v in section.items() if k not in ("adapter", "model", "host")}
    return OllamaLLM(model=section["model"], host=section.get("host", cfg["llm"]["host"]), **rest)

def build_llm(cfg: dict) -> LLMClient:
    section = cfg["llm"]
    if section["adapter"] == "ollama":
        return _build_ollama_llm(section, cfg)
    raise ValueError(f"Unknown llm adapter: {section['adapter']}")

def build_code_model(cfg: dict) -> LLMClient:
    section = cfg["code_model"]
    if section["adapter"] == "ollama":
        return _build_ollama_llm(section, cfg)
    raise ValueError(f"Unknown code_model adapter: {section['adapter']}")

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
        from product.code_edit import apply_plan, commit_changes, rollback_files
        from product.planning import make_plan
        tools = {t.name: t for t in build_tools(cfg)}
        return LangGraphEngine(
            llm=llm or build_llm(cfg),
            retriever=retriever or build_retriever(cfg),
            k=cfg["retriever"].get("top_k", 8),
            checkpoint_path=section.get("checkpoint_path"),
            code_model=build_code_model(cfg),
            read_tool=tools.get("read_file"),
            write_tool=tools.get("write_file"),
            prepare_tool=tools.get("prepare_workspace"),
            run_tests_tool=tools.get("run_tests"),
            commit_tool=tools.get("commit"),
            plan_fn=make_plan,
            apply_plan_fn=apply_plan,
            commit_fn=commit_changes,
            rollback_fn=rollback_files,
            repo_path=cfg["forge"]["repo_path"],
            workspace=cfg["tools"]["workspace"],
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
    if adapter != "mcp":
        raise ValueError(f"Unknown tools adapter: {adapter}")
    from adapters.tools_mcp import load_mcp_tools
    require_approval_for = set(cfg["forge"]["require_approval_for"])
    tools = load_mcp_tools(
        command=sys.executable,
        args=[section["server_script"], section["workspace"]],
        require_approval_for=require_approval_for,
    )
    tools.append(build_open_pr_tool(cfg, require_approval_for))
    return tools

def build_open_pr_tool(cfg: dict, require_approval_for: set[str] | None = None) -> Tool:
    from adapters.github_pr import OpenPrTool
    require_approval_for = require_approval_for or set(cfg["forge"]["require_approval_for"])
    return OpenPrTool(
        workspace=cfg["tools"]["workspace"],
        token=cfg["github"]["token"],
        api_base=cfg["github"].get("api_base", "https://api.github.com"),
        requires_approval="open_pr" in require_approval_for,
    )

def build_ingest(cfg: dict) -> IngestSource:
    section = cfg["ingest"]
    adapter = section["adapter"]
    if adapter == "fs":
        from adapters.ingest_fs import FsIngestSource
        return FsIngestSource(repo_path=cfg["forge"]["repo_path"], languages=cfg["forge"]["languages"])
    raise ValueError(f"Unknown ingest adapter: {adapter}")
