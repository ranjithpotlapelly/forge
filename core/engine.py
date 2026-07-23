"""Port: the orchestration engine that turns a question into a cited answer.
Adapters: LangGraph (local graph execution) -> a hosted agent runtime (upgrade).
"""
from __future__ import annotations
from typing import Protocol, runtime_checkable
from core.types import Answer

@runtime_checkable
class Engine(Protocol):
    def ask(self, question: str, thread_id: str = "default") -> Answer:
        """Run the decision graph for a single question and return a cited answer.

        thread_id scopes checkpointed graph state so separate conversations
        don't bleed into each other; reusing it resumes the same thread.
        """
        ...
