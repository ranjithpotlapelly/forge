"""Port (Phase 19): a durable audit trail of graph runs and their steps.

Deliberately separate from core.store.StateStore (generic product
key-value data, e.g. chat logs) and from the engine's own LangGraph
checkpointer (conversational state for resuming a thread). This is for
listing/inspecting past runs after the fact: what ran, each node's outcome,
and -- most importantly -- every approval decision made along the way, with
what was proposed and what the human chose.
"""
from __future__ import annotations
from typing import Any, Protocol, runtime_checkable

@runtime_checkable
class RunHistory(Protocol):
    def start_run(self, thread_id: str, kind: str, task_text: str) -> str:
        """Record a new run starting now (status='running'). Returns its id."""
        ...

    def finish_run(self, run_id: str, status: str, error: str | None = None) -> None:
        """Record a run's terminal status, e.g. 'completed' or 'failed'."""
        ...

    def record_step(
        self, run_id: str, node: str, status: str,
        detail: str | None = None, duration_ms: float | None = None,
    ) -> None:
        """Record one graph node's execution as part of a run. For an
        approval node, detail carries what was proposed and what was
        decided (JSON), not just pass/fail."""
        ...

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Most recent runs first."""
        ...

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        ...

    def list_steps(self, run_id: str) -> list[dict[str, Any]]:
        """A run's steps in the order they happened."""
        ...
