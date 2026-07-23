"""Port: durable application/business state (tasks, runs, outputs).

Note: the agent graph's conversational checkpointing is handled separately by
the engine's LangGraph checkpointer (Phase 5). This store is for product data.
"""
from __future__ import annotations
from typing import Protocol, Any, runtime_checkable

@runtime_checkable
class StateStore(Protocol):
    def get(self, key: str) -> Any | None: ...
    def put(self, key: str, value: Any) -> None: ...
    def list(self, prefix: str = "") -> list[str]: ...
