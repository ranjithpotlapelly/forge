"""Phase 23 (perf tuning) benchmark: A/B compare Ollama answer-path latency
across --model / --num-ctx / --top-k without touching config.yaml.

Talks to Ollama only through adapters/llm_ollama.py's OllamaLLM (the one file
allowed to), so a bench run and a real Q&A answer send requests the exact
same way -- num_ctx and keep_alive always in the outgoing request, same
build_answer_messages prompt shape, same <think>-stripping. Retrieval for a
given --top-k is run once and reused across --runs generations for that
combination, since the point is comparing generation speed, not adding
retrieval-timing noise into the model/num_ctx comparison.

Usage (from the repo root):
    python -m app.bench --model qwen3:4b --num-ctx 8192 --top-k 5
    python -m app.bench --model qwen3:8b --num-ctx 16384 --top-k 8

Multiple values for any flag compare all combinations in one table:
    python -m app.bench --model qwen3:4b --model qwen3:8b --num-ctx 8192 --num-ctx 16384
"""
from __future__ import annotations
import argparse
import statistics
import sys
import time
from itertools import product

from adapters.engine_langgraph import build_answer_messages
from adapters.llm_ollama import OllamaLLM
from app.config_loader import load_config
from app.wiring import build_retriever

_DEFAULT_QUESTION = "How does the hybrid retriever combine lexical and semantic search results?"


def _run_once(llm: OllamaLLM, messages, num_ctx: int, keep_alive: str) -> tuple[float, float, str]:
    """Returns (ttft_seconds, total_seconds, full_text) for one generation."""
    t0 = time.monotonic()
    ttft = None
    parts: list[str] = []
    for token in llm.stream(messages, num_ctx=num_ctx, keep_alive=keep_alive):
        if ttft is None:
            ttft = time.monotonic() - t0
        parts.append(token)
    total = time.monotonic() - t0
    if ttft is None:  # empty response -- still a valid (if useless) measurement
        ttft = total
    return ttft, total, "".join(parts)


def _bench_combo(
    model: str, num_ctx: int, top_k: int, question: str, runs: int,
    messages_by_top_k: dict[int, list], host: str, keep_alive: str,
) -> dict:
    llm = OllamaLLM(model=model, host=host)
    messages = messages_by_top_k[top_k]
    ttfts, totals, tok_s_list = [], [], []
    for _ in range(runs):
        ttft, total, text = _run_once(llm, messages, num_ctx, keep_alive)
        completion_tokens_est = max(1, len(text) // 4)
        ttfts.append(ttft)
        totals.append(total)
        tok_s_list.append(completion_tokens_est / total if total > 0 else 0.0)
    return {
        "model": model, "num_ctx": num_ctx, "top_k": top_k,
        "median_ttft": statistics.median(ttfts),
        "median_total": statistics.median(totals),
        "median_tok_s": statistics.median(tok_s_list),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", action="append", dest="models", help="repeatable; default: config's answer_model")
    parser.add_argument("--num-ctx", action="append", type=int, dest="num_ctxs", help="repeatable; default: config's answer_num_ctx")
    parser.add_argument("--top-k", action="append", type=int, dest="top_ks", help="repeatable; default: config's answer_top_k")
    parser.add_argument("--question", default=_DEFAULT_QUESTION)
    parser.add_argument("--runs", "-n", type=int, default=3, help="generations per combination (default: 3)")
    args = parser.parse_args(argv)

    cfg = load_config()
    models = args.models or [cfg["answer_model"]["model"]]
    num_ctxs = args.num_ctxs or [cfg["llm"]["answer_num_ctx"]]
    top_ks = args.top_ks or [cfg["retriever"].get("answer_top_k", 5)]
    host = cfg["llm"]["host"]
    keep_alive = cfg["llm"].get("keep_alive", "30m")
    max_expanded = cfg["retriever"].get("answer_max_expanded", 4)

    retriever = build_retriever(cfg)
    messages_by_top_k = {}
    for top_k in set(top_ks):
        t0 = time.monotonic()
        chunks = retriever.search(args.question, top_k)
        retrieve_s = time.monotonic() - t0
        print(f"[retrieve] top_k={top_k}: {len(chunks)} chunk(s) in {retrieve_s:.2f}s")
        messages_by_top_k[top_k] = build_answer_messages(args.question, chunks, max_expanded=max_expanded)

    print(f"\nQuestion: {args.question!r}")
    print(f"Runs per combination: {args.runs}\n")

    results = []
    for model, num_ctx, top_k in product(models, num_ctxs, top_ks):
        print(f"[bench] model={model} num_ctx={num_ctx} top_k={top_k} ...", flush=True)
        results.append(_bench_combo(model, num_ctx, top_k, args.question, args.runs, messages_by_top_k, host, keep_alive))

    header = f"{'model':<16} {'num_ctx':>8} {'top_k':>6} {'ttft (med)':>12} {'total (med)':>12} {'tok/s (med)':>12}"
    print(f"\n{header}")
    print("-" * len(header))
    for r in results:
        print(
            f"{r['model']:<16} {r['num_ctx']:>8} {r['top_k']:>6} "
            f"{r['median_ttft']:>11.2f}s {r['median_total']:>11.2f}s {r['median_tok_s']:>11.1f} "
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
