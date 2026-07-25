"""Forge domain models. Business logic only — no infra imports."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

@dataclass
class CodeUnit:
    """A structure-aware slice of the repo (function / class / module)."""
    path: str
    symbol: str
    language: str
    start_line: int
    end_line: int
    code: str

@dataclass
class Issue:
    number: int
    title: str
    body: str

# Consumed by the plan node the planner phase will add — not wired to
# anything yet.
@dataclass
class PlanStep:
    description: str
    target_path: str
    kind: Literal["edit", "create", "delete", "run_tests"]

@dataclass
class ProposedPR:
    """Forge proposes; a human approves; only then is it opened."""
    title: str
    body: str
    branch: str
    steps: list[PlanStep] = field(default_factory=list)
