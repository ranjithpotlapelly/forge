"""Prompt 26: a single pass/fail regression gate over eval quality, wiring
eval/run.py's retrieval metrics (and optionally eval/judge.py's
answer-groundedness score) against named thresholds in eval/thresholds.yaml.

    python -m eval.gate [--with-judge] [--thresholds PATH] [--config PATH]
                         [--dataset PATH] [--k N]

WHY: eval/run.py and eval/judge.py already measure quality; this turns that
measurement into a build-blocking decision -- "gate merges on retrieval
hit-rate and answer groundedness" -- by comparing the SAME metrics against
named thresholds and returning one pass/fail exit code CI can key off.

This module only calls through eval.run's and eval.judge's existing public
functions (run_suite, Metrics, build_judge_llm, ...) and app.wiring/core --
it doesn't duplicate their scoring logic, and it doesn't modify core/,
product/, any existing adapter, or app/ask.py.

Skippable locally: this is a plain CLI, not wired into any git hook, so it
never runs unless invoked directly -- and setting SKIP_EVAL_GATE=1 makes it
a no-op (exit 0) even then, in case something later does wire it into a hook.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.config_loader import load_config
from app.wiring import build_answer_model, build_retriever
from eval.dataset import DEFAULT_DATASET, load_dataset
from eval.judge import build_judge_llm
from eval.judge import print_averages as print_judge_averages
from eval.judge import print_case_table as print_judge_case_table
from eval.judge import run_suite as run_judge_suite
from eval.run import Metrics
from eval.run import print_case_table as print_retrieval_case_table
from eval.run import run_suite as run_retrieval_suite

DEFAULT_THRESHOLDS = Path(__file__).parent / "thresholds.yaml"


@dataclass
class ThresholdCheck:
    name: str
    value: float
    threshold: float

    @property
    def passed(self) -> bool:
        return self.value >= self.threshold


def load_thresholds(path: Path) -> dict:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a top-level mapping of threshold_name: value")
    return raw


def _check(name: str, value: float, thresholds: dict, key: str) -> ThresholdCheck | None:
    """None means "not gated" -- the key is simply absent from thresholds.yaml."""
    threshold = thresholds.get(key)
    if threshold is None:
        return None
    return ThresholdCheck(name=name, value=value, threshold=float(threshold))


def print_gate_table(checks: list[ThresholdCheck]) -> None:
    print("\n=== Gate ===")
    if not checks:
        print("(no thresholds configured -- nothing gated)")
        return
    width = max(len(c.name) for c in checks)
    for c in checks:
        status = "PASS" if c.passed else "FAIL"
        print(f"{status:<6} {c.name:<{width}}  {c.value:.3f}  (threshold >= {c.threshold:.3f})")


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("SKIP_EVAL_GATE"):
        print("[skip] SKIP_EVAL_GATE is set -- not running the eval gate.")
        return 0

    # A judge's `reasoning` text is free to contain Unicode punctuation
    # Windows' default console codepage can't encode -- see eval/judge.py's
    # main() for the same fix.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="path to dataset.yaml (default: eval/dataset.yaml)")
    parser.add_argument("--config", default="config.yaml", help="config.yaml to build the retriever/models from (default: config.yaml)")
    parser.add_argument("--thresholds", default=str(DEFAULT_THRESHOLDS), help="path to thresholds.yaml (default: eval/thresholds.yaml)")
    parser.add_argument("--k", type=int, default=None, help="top-k for retrieval (default: retriever.top_k from config)")
    parser.add_argument("--with-judge", action="store_true", help="also run eval.judge's answer-groundedness check and gate on min_groundedness_avg")
    args = parser.parse_args(argv)

    thresholds = load_thresholds(Path(args.thresholds))
    cfg = load_config(args.config)
    cases = load_dataset(Path(args.dataset))
    if not cases:
        print(f"[!!] no cases found in {args.dataset}")
        return 1

    k = args.k if args.k is not None else cfg["retriever"].get("top_k", 8)
    retriever = build_retriever(cfg)

    print(f"=== Retrieval (k={k}) ===")
    retrieval_results = run_retrieval_suite(retriever, cases, k)
    print_retrieval_case_table(retrieval_results)
    metrics = Metrics.from_results(retrieval_results, k)
    print(f"\nhit@{k}: {metrics.hit_at_k:.3f}  MRR: {metrics.mrr:.3f}  precision@{k}: {metrics.precision_at_k:.3f}")

    checks: list[ThresholdCheck] = []
    for check in (
        _check("hit@k", metrics.hit_at_k, thresholds, "min_hit_at_k"),
        _check("MRR", metrics.mrr, thresholds, "min_mrr"),
    ):
        if check:
            checks.append(check)

    if args.with_judge:
        judged_cases = [c for c in cases if c.reference]
        if not judged_cases:
            print(f"\n[!!] --with-judge given but no cases in {args.dataset} have a 'reference' field")
            return 1
        answer_llm = build_answer_model(cfg)
        judge_llm, judge_label = build_judge_llm(cfg)
        answer_k = cfg["retriever"].get("answer_top_k", 5)
        max_expanded = cfg["retriever"].get("answer_max_expanded")
        print(f"\n=== Judge (answering with answer_model ({answer_llm.model}), judging with {judge_label}) ===")
        judge_results = run_judge_suite(cases, retriever, answer_llm, judge_llm, answer_k, max_expanded)
        print_judge_case_table(judge_results)
        print_judge_averages(judge_results)

        errors = [r for r in judge_results if r.status == "error"]
        if errors:
            print(f"\n[FAIL] {len(errors)} case(s) could not be judged (malformed judge output, twice)")
            return 1

        scored = [r for r in judge_results if r.status == "scored"]
        groundedness_avg = (sum(r.groundedness for r in scored) / len(scored)) if scored else 0.0
        grounded_check = _check("groundedness_avg", groundedness_avg, thresholds, "min_groundedness_avg")
        if grounded_check:
            checks.append(grounded_check)

    print_gate_table(checks)
    failed = [c for c in checks if not c.passed]
    if failed:
        print(f"\n[FAIL] {len(failed)}/{len(checks)} threshold(s) not met: {', '.join(c.name for c in failed)}")
        return 1
    print(f"\n[ok] {len(checks)}/{len(checks)} threshold(s) met.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
