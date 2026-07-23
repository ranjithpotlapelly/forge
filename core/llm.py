"""Port: text generation. Adapters: Ollama (local) -> hosted API (upgrade)."""
from __future__ import annotations
from typing import Protocol, Iterable, runtime_checkable
from core.types import Message

@runtime_checkable
class LLMClient(Protocol):
    def generate(self, messages: list[Message], **opts) -> str:
        """Return a single completion for the given messages."""
        ...

    def stream(self, messages: list[Message], **opts) -> Iterable[str]:
        """Yield tokens/segments as they are produced."""
        ...
