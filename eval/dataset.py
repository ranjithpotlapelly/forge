"""Shared dataset model for eval/dataset.yaml, used by both eval/run.py
(retrieval scoring, Prompt 23) and eval/judge.py (answer-quality scoring,
Prompt 24) -- one Case shape, two different things scored off it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_DATASET = Path(__file__).parent / "dataset.yaml"


@dataclass
class Case:
    question: str
    expect_files: list[str] = field(default_factory=list)
    expect_symbols: list[str] = field(default_factory=list)
    # Below: optional, judge-only (eval/judge.py). A case with no `reference`
    # has nothing to check an answer against, so eval/judge.py skips it --
    # eval/run.py never looks at either field.
    reference: str | None = None
    force_answer: str | None = None

    @staticmethod
    def from_dict(d: dict) -> "Case":
        if not d.get("question"):
            raise ValueError(f"dataset case missing 'question': {d!r}")
        if not d.get("expect_files"):
            raise ValueError(f"case {d['question']!r} missing 'expect_files'")
        return Case(
            question=d["question"],
            expect_files=list(d["expect_files"]),
            expect_symbols=list(d.get("expect_symbols") or []),
            reference=d.get("reference") or None,
            force_answer=d.get("force_answer") or None,
        )


def load_dataset(path: Path) -> list[Case]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected a top-level list of cases")
    return [Case.from_dict(d) for d in raw]
