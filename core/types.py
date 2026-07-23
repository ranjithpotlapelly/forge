"""Shared value types passed across ports. No vendor or domain imports."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]

@dataclass
class Message:
    role: Role
    content: str

@dataclass
class Document:
    """A unit of knowledge entering the system (e.g. a code chunk)."""
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class Chunk:
    """A retrieved piece of context with source metadata and a score."""
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float | None = None

@dataclass
class Answer:
    """Result of asking the engine a question: the text plus the chunks it cites."""
    text: str
    citations: list[Chunk] = field(default_factory=list)
