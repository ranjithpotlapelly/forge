"""Approval gate (Phase 7): decides whether a tool call must pause for human
sign-off before it runs, per forge.require_approval_for in config.yaml.

How approval is actually collected (CLI prompt, UI button, auto-approve in
tests) is the caller's business — this only enforces the gate.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable, Literal
from core.tools import Tool

Approve = Callable[[Tool, dict[str, Any]], bool]

class ApprovalDenied(Exception):
    """Raised when a human explicitly rejects a gated tool call."""

def run_tool(tool: Tool, kwargs: dict[str, Any], approve: Approve) -> Any:
    if tool.requires_approval and not approve(tool, kwargs):
        raise ApprovalDenied(f"{tool.name} was not approved to run with {kwargs!r}")
    return tool.run(**kwargs)

# --- plan-level approval (Phase 17) -----------------------------------------
# A distinct, richer decision surface from Approve/run_tool above: a
# ProposedPR isn't a Tool call (no requires_approval flag, no single kwargs
# dict), and unlike a tool call it can come back "not quite — fix this and
# ask me again" rather than a flat yes/no. This is additive: Approve/run_tool/
# ApprovalDenied are unchanged, so every existing tool-level caller (write_file/
# commit/push/open_pr, and the tests that gate them) keeps working exactly as
# before. The UI layer (app/chainlit_app.py) is one implementation of
# PlanApprove; a terminal prompt or a test's lambda are others.

PlanDecisionKind = Literal["approve", "edit", "reject"]

@dataclass
class PlanDecision:
    decision: PlanDecisionKind
    feedback: str | None = None  # only meaningful when decision == "edit"

PlanApprove = Callable[[Any], PlanDecision]  # Any = product.schema.ProposedPR
