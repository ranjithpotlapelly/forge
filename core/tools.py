"""Port: an action the agent can take. Adapters wrap MCP servers or local fns.

Tools that mutate the outside world set requires_approval=True so the engine
pauses for human sign-off before running them (Phase 7).
"""
from __future__ import annotations
from typing import Protocol, Any, runtime_checkable

@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    requires_approval: bool

    def input_schema(self) -> dict[str, Any]:
        """JSON schema describing the tool's arguments (for the LLM)."""
        ...

    def run(self, **kwargs) -> Any:
        """Execute the tool and return a result."""
        ...
