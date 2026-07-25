"""Planning workflow (Phase 14): asks the primary LLM for a structured
ProposedPR (JSON), validates it into product/schema.py's dataclasses, and
rejects any step whose target_path could escape the workspace sandbox.

Deliberately not imported by adapters/engine_langgraph.py — see that file's
module docstring. app/wiring.py injects make_plan into LangGraphEngine as a
plain callable so the adapter never has to import product/.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from core.llm import LLMClient
from core.types import Chunk, Message
from product.schema import PlanStep, ProposedPR

_PROMPT_PATH = Path(__file__).parent / "prompts" / "plan.txt"
_VALID_KINDS = {"edit", "create", "delete", "run_tests"}

class PlanValidationError(Exception):
    """Raised when the model's plan can't be parsed or fails validation."""

def _validate_target_path(target_path: str, kind: str, known_paths: set[str]) -> None:
    if not target_path or target_path.strip() != target_path:
        raise PlanValidationError(f"empty or malformed target_path: {target_path!r}")
    p = Path(target_path)
    if p.is_absolute():
        raise PlanValidationError(f"target_path must be relative, got an absolute path: {target_path!r}")
    if ".." in p.parts:
        raise PlanValidationError(f"target_path must not contain '..': {target_path!r}")
    # "create" is the only kind allowed to name a path that doesn't exist yet.
    # Models reliably hallucinate plausible-looking paths (e.g. inventing a
    # "src/" prefix that doesn't match the actual repo layout) — caught here
    # rather than silently editing/creating the wrong file, which the
    # downstream splice/write logic can't detect on its own since a wrong
    # path just looks like "file doesn't exist yet".
    if kind != "create" and known_paths and target_path not in known_paths:
        raise PlanValidationError(
            f"target_path {target_path!r} (kind={kind!r}) is not one of the "
            f"actually-retrieved files: {sorted(known_paths)}"
        )

def _parse_plan(raw: str, known_paths: set[str]) -> ProposedPR:
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError as e:
        raise PlanValidationError(f"model did not return valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise PlanValidationError(f"expected a JSON object, got {type(data).__name__}")

    missing = {"title", "body", "branch", "steps"} - data.keys()
    if missing:
        raise PlanValidationError(f"plan JSON is missing required key(s): {sorted(missing)}")

    raw_steps = data["steps"]
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanValidationError("plan must have a non-empty 'steps' list")

    steps: list[PlanStep] = []
    for i, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, dict):
            raise PlanValidationError(f"step {i} is not an object: {raw_step!r}")
        step_missing = {"description", "target_path", "kind"} - raw_step.keys()
        if step_missing:
            raise PlanValidationError(f"step {i} is missing key(s): {sorted(step_missing)}")
        kind = raw_step["kind"]
        if kind not in _VALID_KINDS:
            raise PlanValidationError(f"step {i} has invalid kind {kind!r}, expected one of {sorted(_VALID_KINDS)}")
        target_path = raw_step["target_path"]
        _validate_target_path(target_path, kind, known_paths)
        steps.append(PlanStep(description=raw_step["description"], target_path=target_path, kind=kind))

    return ProposedPR(title=data["title"], body=data["body"], branch=data["branch"], steps=steps)

def _build_messages(
    task: str, files: list[Chunk], known_paths: set[str],
    error: str | None = None, feedback: str | None = None,
) -> list[Message]:
    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    file_sections = "\n\n".join(
        f"--- {f.metadata.get('path', '?')} ---\n{f.content}" for f in files
    ) or "(no files retrieved)"
    paths_list = "\n".join(f"- {p}" for p in sorted(known_paths)) or "(none)"
    user_content = (
        f"Task: {task}\n\n"
        f"target_path for any 'edit' or 'delete' step MUST be exactly one of "
        f"these paths (copy it verbatim — do not add, remove, or guess at "
        f"directory prefixes):\n{paths_list}\n\n"
        f"Relevant files:\n{file_sections}"
    )
    if feedback:
        user_content += (
            f"\n\nA human reviewed a previous version of this plan and asked "
            f"for changes before it can be approved: {feedback}\n"
            "Produce a revised plan that addresses this."
        )
    if error:
        user_content += (
            f"\n\nYour previous response was invalid: {error}\n"
            "Respond again with ONLY the corrected JSON object."
        )
    return [
        Message(role="system", content=system_prompt),
        Message(role="user", content=user_content),
    ]

def make_plan(llm: LLMClient, task: str, files: list[Chunk], feedback: str | None = None) -> ProposedPR:
    """Ask the LLM for a plan; retry once with the parse/validation error
    appended if the first response doesn't parse, then give up clearly.

    feedback carries a human's requested correction from a previous round
    (Chainlit's "Edit" action, or any other PlanApprove implementation) --
    plumbed into every retry's prompt too, so a parse-retry doesn't silently
    drop the revision the human actually asked for.
    """
    known_paths = {f.metadata.get("path") for f in files if f.metadata.get("path")}
    messages = _build_messages(task, files, known_paths, feedback=feedback)
    raw = llm.generate(messages, format="json")
    try:
        return _parse_plan(raw, known_paths)
    except PlanValidationError as first_error:
        retry_messages = _build_messages(task, files, known_paths, error=str(first_error), feedback=feedback)
        raw = llm.generate(retry_messages, format="json")
        try:
            return _parse_plan(raw, known_paths)
        except PlanValidationError as second_error:
            raise PlanValidationError(
                f"model failed to produce a valid plan after one retry. "
                f"First error: {first_error}. Second error: {second_error}. "
                f"Last raw response: {raw!r}"
            ) from second_error
