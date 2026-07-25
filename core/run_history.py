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

    def find_running_run(self, thread_id: str) -> str | None:
        """The most recent still-'running' run for a thread, or None.

        (Phase 20) Lets a resumed run -- possibly in a brand new process,
        with no in-memory link back to the run_id its original run_task()
        call created -- keep recording run_steps under that same run
        instead of losing the correlation or minting a misleading new one.
        """
        ...

    def list_threads(self, limit: int = 20) -> list[dict[str, Any]]:
        """One row per distinct thread_id, most recently active first --
        the "conversation list" a UI shows (Phase 21). Each row:
        thread_id, title (the first run's task_text, i.e. the first user
        message), kind, run_count, started_at (first run), last_active_at
        (most recent run), status (of the most recent run), latest_run_id.

        A thread_id is homogeneous by construction (a Q&A session's
        thread_id accumulates multiple "qa" runs; a task's thread_id is
        unique to that one task and never reused), so "kind"/"status" here
        unambiguously describe the whole thread, not just one run of it.
        """
        ...

    def list_runs_for_thread(self, thread_id: str) -> list[dict[str, Any]]:
        """Every run for a thread_id, oldest first -- e.g. every question
        asked in one Q&A session, in order, to replay as a transcript."""
        ...

    def delete_thread(self, thread_id: str) -> int:
        """Delete every run (and its steps) for a thread_id. Returns the
        number of runs deleted. Does not touch the checkpointer -- pair with
        LangGraphEngine.delete_thread() (adapters/engine_langgraph.py) to
        also remove its checkpoints, per this port's own separation from
        that other kind of per-thread state (see module docstring)."""
        ...
