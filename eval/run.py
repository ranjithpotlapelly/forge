"""Retrieval-quality evaluation harness (additive, self-contained).

    python -m eval.run [--k N] [--config PATH] [--dataset PATH] [--min-hit-at-k F]
    python -m eval.run --compare [--compare-k N] [--compare-config PATH]

Runs eval/dataset.yaml's questions through the EXISTING retriever
(app.wiring.build_retriever -> core.retriever.Retriever.search, whatever
adapter config.yaml currently wires in) and checks whether the known-correct
file/symbol for each question comes back in the top-k results.

WHY retrieval and not answer text: retrieval has a definite right answer --
did the correct file/symbol come back -- so it can be scored automatically.
Judging prose quality is subjective and comes later, on top of this.

This module only reads through core.retriever.Retriever (via
app.wiring.build_retriever) and core.types.Chunk. It does not import or
modify core/, product/, adapters/, or app/ask.py -- it's a new, independent
consumer of the same port everything else already uses.

Exit code: 0 if hit@k >= --min-hit-at-k (default 0.7), 1 otherwise -- wire
this into CI as a retrieval-quality regression gate. --compare mode always
exits 0 (it's a comparison report, not a gate) unless the run itself errors.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from app.config_loader import load_config
from app.wiring import build_retriever
from core.retriever import Retriever
from core.types import Chunk
from eval.dataset import DEFAULT_DATASET as _DEFAULT_DATASET
from eval.dataset import Case, load_dataset


def _is_relevant(chunk: Chunk, case: Case) -> bool:
    """A chunk counts toward a case if its path matches one of expect_files
    (substring, case-sensitive -- real paths are). If expect_symbols is also
    given, the symbol must ADDITIONALLY match one of them (substring,
    case-insensitive) -- that's what pins a hit to one chunk within a
    multi-symbol file instead of any chunk from the right file counting."""
    path = str(chunk.metadata.get("path", ""))
    if not any(f in path for f in case.expect_files):
        return False
    if case.expect_symbols:
        symbol = str(chunk.metadata.get("symbol", "")).lower()
        if not any(s.lower() in symbol for s in case.expect_symbols):
            return False
    return True


@dataclass
class CaseResult:
    case: Case
    rank: int | None       # 1-based rank of the first relevant chunk, None if none found
    relevant_in_topk: int  # count of relevant chunks anywhere in the top-k
    returned: int          # chunks actually returned (may be < k for a small index)

    @property
    def hit(self) -> bool:
        return self.rank is not None


def run_case(retriever: Retriever, case: Case, k: int) -> CaseResult:
    results = retriever.search(case.question, k=k)
    rank = None
    relevant = 0
    for i, chunk in enumerate(results, start=1):
        if _is_relevant(chunk, case):
            relevant += 1
            if rank is None:
                rank = i
    return CaseResult(case=case, rank=rank, relevant_in_topk=relevant, returned=len(results))


def run_suite(retriever: Retriever, cases: list[Case], k: int) -> list[CaseResult]:
    return [run_case(retriever, case, k) for case in cases]


@dataclass
class Metrics:
    k: int
    n_cases: int
    hit_at_k: float
    mrr: float
    precision_at_k: float

    @staticmethod
    def from_results(results: list[CaseResult], k: int) -> "Metrics":
        n = len(results)
        if n == 0:
            return Metrics(k=k, n_cases=0, hit_at_k=0.0, mrr=0.0, precision_at_k=0.0)
        hits = sum(1 for r in results if r.hit)
        rr_sum = sum(1.0 / r.rank for r in results if r.rank is not None)
        # precision@k divides by the cutoff k (standard definition), not by
        # however many chunks actually came back -- a case that only got 3
        # results because the index is small still scores out of k.
        precision_sum = sum(r.relevant_in_topk / k for r in results)
        return Metrics(k=k, n_cases=n, hit_at_k=hits / n, mrr=rr_sum / n, precision_at_k=precision_sum / n)


def print_case_table(results: list[CaseResult]) -> None:
    q_width = min(64, max((len(r.case.question) for r in results), default=8))
    header = f"{'result':<6} {'rank':>4}  {'question':<{q_width}}"
    print(header)
    print("-" * len(header))
    for r in results:
        status = "PASS" if r.hit else "FAIL"
        rank = str(r.rank) if r.rank is not None else "-"
        q = r.case.question if len(r.case.question) <= q_width else r.case.question[: q_width - 3] + "..."
        print(f"{status:<6} {rank:>4}  {q:<{q_width}}")


def print_metrics(label: str, m: Metrics) -> None:
    print(f"\n{label}  (k={m.k}, n={m.n_cases})")
    print(f"  hit@{m.k}:        {m.hit_at_k:.3f}")
    print(f"  MRR:           {m.mrr:.3f}")
    print(f"  precision@{m.k}:  {m.precision_at_k:.3f}")


def print_compare_table(label_a: str, m_a: Metrics, label_b: str, m_b: Metrics) -> None:
    col = max(18, len(label_a) + 2, len(label_b) + 2)
    header = f"{'metric':<14} {label_a:>{col}} {label_b:>{col}}"
    print(f"\n{header}")
    print("-" * len(header))
    print(f"{'hit@k':<14} {m_a.hit_at_k:>{col}.3f} {m_b.hit_at_k:>{col}.3f}")
    print(f"{'MRR':<14} {m_a.mrr:>{col}.3f} {m_b.mrr:>{col}.3f}")
    print(f"{'precision@k':<14} {m_a.precision_at_k:>{col}.3f} {m_b.precision_at_k:>{col}.3f}")


def _run_and_report(retriever: Retriever, cases: list[Case], k: int, label: str) -> Metrics:
    results = run_suite(retriever, cases, k)
    print(f"\n=== {label} ===")
    print_case_table(results)
    metrics = Metrics.from_results(results, k)
    print_metrics(label, metrics)
    return metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default=str(_DEFAULT_DATASET), help="path to dataset.yaml (default: eval/dataset.yaml)")
    parser.add_argument("--config", default="config.yaml", help="config.yaml to build the retriever from (default: config.yaml)")
    parser.add_argument("--k", type=int, default=None, help="top-k to evaluate (default: retriever.top_k from config)")
    parser.add_argument("--min-hit-at-k", type=float, default=0.7, help="regression gate: exit 1 if hit@k falls below this (default: 0.7)")
    parser.add_argument("--compare", action="store_true", help="run the suite twice and print a side-by-side comparison instead of gating")
    parser.add_argument("--compare-k", type=int, default=1, help="second top-k for --compare's run B (default: 1; ignored if --compare-config is set and --k is not)")
    parser.add_argument("--compare-config", default=None, help="second config.yaml for --compare's run B, e.g. to compare before/after a chunking change")
    args = parser.parse_args(argv)

    if args.k is not None and args.k < 1:
        parser.error("--k must be >= 1")
    if args.compare_k < 1:
        parser.error("--compare-k must be >= 1")

    dataset_path = Path(args.dataset)
    cases = load_dataset(dataset_path)
    if not cases:
        print(f"[!!] no cases found in {dataset_path}")
        return 1

    cfg_a = load_config(args.config)
    k_a = args.k if args.k is not None else cfg_a["retriever"].get("top_k", 8)

    if not args.compare:
        retriever = build_retriever(cfg_a)
        metrics = _run_and_report(retriever, cases, k_a, args.config)
        if metrics.hit_at_k < args.min_hit_at_k:
            print(f"\n[FAIL] hit@{k_a}={metrics.hit_at_k:.3f} is below threshold {args.min_hit_at_k:.3f}")
            return 1
        print(f"\n[ok] hit@{k_a}={metrics.hit_at_k:.3f} meets threshold {args.min_hit_at_k:.3f}")
        return 0

    # --compare: two runs, side by side. Either two top_k values against the
    # same index (default), or two configs (e.g. before/after a chunking
    # change) each at the same k -- --compare-config wins if both are set.
    retriever_a = build_retriever(cfg_a)
    label_a = f"A: {args.config} (k={k_a})"
    metrics_a = _run_and_report(retriever_a, cases, k_a, label_a)

    if args.compare_config:
        cfg_b = load_config(args.compare_config)
        retriever_b = build_retriever(cfg_b)
        k_b = args.k if args.k is not None else cfg_b["retriever"].get("top_k", 8)
        label_b = f"B: {args.compare_config} (k={k_b})"
    else:
        retriever_b = retriever_a
        k_b = args.compare_k
        label_b = f"B: {args.config} (k={k_b})"
    metrics_b = _run_and_report(retriever_b, cases, k_b, label_b)

    print_compare_table(label_a, metrics_a, label_b, metrics_b)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
