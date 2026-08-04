"""LLM-as-judge: scores ANSWER quality (additive, self-contained).

    python -m eval.judge [--k N] [--config PATH] [--dataset PATH]

Complements eval/run.py (Prompt 23), which only checks whether the right
file/symbol was RETRIEVED. This checks whether the ANSWER generated from
that retrieval is actually correct and grounded in the cited source -- the
part string-matching can't score, since two answers can both mention the
right symbol while only one of them is telling the truth about it.

For each eval/dataset.yaml case that has a `reference` field (a case with
none is skipped -- nothing to check an answer against): retrieves and
generates an answer the exact same way app/chainlit_app.py's live Q&A path
does (same adapters.engine_langgraph.has_context/build_answer_messages, same
Retriever.search, same answer_model) -- or, if the case sets `force_answer`,
judges that fixed text instead of generating one, so a known-bad answer can
be exercised deterministically without depending on a model actually
hallucinating on cue.

The judge model then scores QUESTION + ANSWER + CITED SOURCE + REFERENCE on
three 1-5 axes (correctness, groundedness, relevance) and must return strict
JSON -- malformed output gets one retry with a corrective follow-up message,
then the case is reported as a hard failure rather than crashing the run.

This module only reads through core.llm.LLMClient and core.retriever.Retriever
(via app.wiring) and imports read-only from adapters.engine_langgraph (the
same functions app/chainlit_app.py already imports from it, per its own
module docstring) -- it does not modify core/, product/, any existing
adapter, or app/ask.py.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from adapters.engine_langgraph import build_answer_messages, has_context
from app.config_loader import load_config
from app.wiring import build_answer_model, build_code_model, build_llm, build_retriever
from core.llm import LLMClient
from core.retriever import Retriever
from core.types import Message
from eval.dataset import DEFAULT_DATASET, Case, load_dataset

# A case scoring at or below this on groundedness gets flagged for review --
# not a hard gate (unlike eval/run.py's hit@k threshold), since a low score
# here means "look at this," not "the harness is definitely broken."
_HALLUCINATION_THRESHOLD = 2

_JUDGE_SYSTEM_PROMPT = (
    "You are a strict, careful code-QA judge. You are given a QUESTION about "
    "a codebase, a generated ANSWER, the CITED SOURCE code the answer is "
    "supposed to be grounded in, and a REFERENCE note describing the correct "
    "answer.\n\n"
    "Score the ANSWER on three axes, each an integer from 1 (worst) to 5 "
    "(best):\n"
    "- correctness: is the answer factually right, compared to the REFERENCE?\n"
    "- groundedness: is every claim in the answer actually supported by the "
    "CITED SOURCE? A claim not backed by the cited code -- even if it sounds "
    "plausible -- is a hallucination and must be scored low, regardless of "
    "how correct it happens to be.\n"
    "- relevance: does the answer actually address the QUESTION asked, "
    "rather than something adjacent or generic?\n\n"
    "Respond with STRICT JSON ONLY -- no markdown fences, no commentary "
    "outside the JSON -- in exactly this shape:\n"
    '{"correctness": <1-5 int>, "groundedness": <1-5 int>, "relevance": '
    '<1-5 int>, "reasoning": "<one sentence>"}'
)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_AXES = ("correctness", "groundedness", "relevance")


def _judge_messages(question: str, answer_text: str, context_block: str, reference: str) -> list[Message]:
    return [
        Message(role="system", content=_JUDGE_SYSTEM_PROMPT),
        Message(role="user", content=(
            f"QUESTION:\n{question}\n\n"
            f"ANSWER:\n{answer_text}\n\n"
            f"CITED SOURCE:\n{context_block}\n\n"
            f"REFERENCE:\n{reference}"
        )),
    ]


def _context_block(answer_messages: list[Message]) -> str:
    """build_answer_messages()'s user message is "Context:\\n<context>\\n\\n
    Question: <question>" -- reuse that exact context text (what the answer
    model actually saw) rather than re-deriving chunk formatting here."""
    user = answer_messages[1].content
    return user.split("\n\nQuestion:", 1)[0].removeprefix("Context:\n")


def _parse_judgment(raw: str) -> dict | None:
    """Best-effort strict-JSON parse: some models still wrap `format="json"`
    output in a markdown fence despite the instruction, so that's stripped
    before falling back to json.loads, and again to pulling out the first
    {...} block if the model added stray text around the JSON."""
    import json

    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            first_line, rest = text.split("\n", 1)
            text = rest if first_line.strip().lower() in ("", "json") else text
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(text)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    try:
        scores = {axis: int(data[axis]) for axis in _AXES}
    except (KeyError, TypeError, ValueError):
        return None
    if not all(1 <= v <= 5 for v in scores.values()):
        return None
    reasoning = data.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        return None
    return {**scores, "reasoning": reasoning.strip()}


def _judge_once(judge_llm: LLMClient, messages: list[Message]) -> tuple[dict | None, str]:
    raw = judge_llm.generate(messages, format="json")
    return _parse_judgment(raw), raw


def judge_answer(judge_llm: LLMClient, messages: list[Message]) -> tuple[dict | None, str | None]:
    """Returns (judgment, error) -- judgment is None iff error is set.
    One retry on malformed output, with the bad response fed back and an
    explicit correction request, before giving up on this case."""
    judgment, raw = _judge_once(judge_llm, messages)
    if judgment is not None:
        return judgment, None
    retry_messages = messages + [
        Message(role="assistant", content=raw),
        Message(role="user", content=(
            "That response was not valid JSON matching the required schema "
            '({"correctness": int, "groundedness": int, "relevance": int, '
            '"reasoning": str}, each score 1-5). Reply with ONLY the JSON '
            "object, nothing else."
        )),
    ]
    judgment, raw2 = _judge_once(judge_llm, retry_messages)
    if judgment is not None:
        return judgment, None
    return None, f"judge returned malformed JSON twice; last response: {raw2[:200]!r}"


@dataclass
class JudgeResult:
    case: Case
    status: str  # "scored" | "declined" | "error"
    answer_text: str | None = None
    correctness: int | None = None
    groundedness: int | None = None
    relevance: int | None = None
    reasoning: str | None = None
    error: str | None = None

    @property
    def hallucination(self) -> bool:
        return self.groundedness is not None and self.groundedness <= _HALLUCINATION_THRESHOLD


def run_case(
    case: Case, retriever: Retriever, answer_llm: LLMClient, judge_llm: LLMClient,
    k: int, max_expanded: int | None,
) -> JudgeResult:
    chunks = retriever.search(case.question, k=k)
    if has_context({"chunks": chunks}) != "answer":
        return JudgeResult(case=case, status="declined")
    answer_messages = build_answer_messages(case.question, chunks, max_expanded=max_expanded)
    answer_text = case.force_answer or answer_llm.generate(answer_messages)
    context_block = _context_block(answer_messages)
    messages = _judge_messages(case.question, answer_text, context_block, case.reference or "")
    judgment, error = judge_answer(judge_llm, messages)
    if judgment is None:
        return JudgeResult(case=case, status="error", answer_text=answer_text, error=error)
    return JudgeResult(
        case=case, status="scored", answer_text=answer_text,
        correctness=judgment["correctness"], groundedness=judgment["groundedness"],
        relevance=judgment["relevance"], reasoning=judgment["reasoning"],
    )


def run_suite(
    cases: list[Case], retriever: Retriever, answer_llm: LLMClient, judge_llm: LLMClient,
    k: int, max_expanded: int | None,
) -> list[JudgeResult]:
    judged = [c for c in cases if c.reference]
    return [run_case(c, retriever, answer_llm, judge_llm, k, max_expanded) for c in judged]


def print_case_table(results: list[JudgeResult]) -> None:
    q_width = min(50, max((len(r.case.question) for r in results), default=8))
    header = f"{'status':<9} {'corr':>4} {'grnd':>4} {'rlvn':>4}  {'question':<{q_width}}  reasoning"
    print(header)
    print("-" * len(header))
    for r in results:
        q = r.case.question if len(r.case.question) <= q_width else r.case.question[: q_width - 3] + "..."
        if r.status == "scored":
            flag = "  [HALLUCINATION]" if r.hallucination else ""
            print(f"{'SCORED':<9} {r.correctness:>4} {r.groundedness:>4} {r.relevance:>4}  {q:<{q_width}}  {r.reasoning}{flag}")
        elif r.status == "declined":
            print(f"{'DECLINED':<9} {'-':>4} {'-':>4} {'-':>4}  {q:<{q_width}}  retriever found no relevant context -- nothing to judge")
        else:
            print(f"{'ERROR':<9} {'-':>4} {'-':>4} {'-':>4}  {q:<{q_width}}  {r.error}")


def print_averages(results: list[JudgeResult]) -> None:
    scored = [r for r in results if r.status == "scored"]
    if not scored:
        print("\nNo cases were successfully scored.")
        return
    n = len(scored)
    print(f"\nAverages over {n} scored case(s):")
    for axis in _AXES:
        avg = sum(getattr(r, axis) for r in scored) / n
        print(f"  {axis + ':':<14} {avg:.2f}")

    flagged = [r for r in scored if r.hallucination]
    if flagged:
        print(f"\n[HALLUCINATION] {len(flagged)} case(s) flagged for review (groundedness <= {_HALLUCINATION_THRESHOLD}):")
        for r in flagged:
            print(f"  - {r.case.question!r}: groundedness={r.groundedness} -- {r.reasoning}")


def build_judge_llm(cfg: dict) -> tuple[LLMClient, str]:
    """The judge should be at least as capable as the model it's judging
    (build_answer_model -- config.yaml's small/fast answer_model, chosen for
    Q&A latency, not judgment quality) -- a weaker judge is an unreliable
    grader. code_model is config.yaml's strongest/hosted slot, so it's tried
    first; this only falls back to `llm` (the local Ollama model, also used
    for planning/task classification -- stronger than answer_model but still
    local) if code_model isn't configured on this machine (e.g. .env's
    CODE_MODEL_* vars are blank)."""
    try:
        judge = build_code_model(cfg)
        label = f"code_model ({judge.model})"
    except Exception as e:  # noqa: BLE001 -- any construction failure means "not available here"
        print(f"[judge] code_model unavailable ({e}) -- falling back to llm")
        judge = build_llm(cfg)
        label = f"llm ({judge.model})"
    return judge, label


def main(argv: list[str] | None = None) -> int:
    # A judge model is free to emit Unicode punctuation (curly quotes, en/em
    # dashes, non-breaking hyphens, ...) in its `reasoning` text -- Windows'
    # default console codepage (cp1252) can't encode a lot of that and would
    # otherwise crash the whole run on a single un-printable character deep
    # into an otherwise-successful case.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="path to dataset.yaml (default: eval/dataset.yaml)")
    parser.add_argument("--config", default="config.yaml", help="config.yaml to build the retriever/models from (default: config.yaml)")
    parser.add_argument("--k", type=int, default=None, help="top-k for the answer path (default: retriever.answer_top_k from config)")
    args = parser.parse_args(argv)

    if args.k is not None and args.k < 1:
        parser.error("--k must be >= 1")

    dataset_path = Path(args.dataset)
    cases = load_dataset(dataset_path)
    judged_cases = [c for c in cases if c.reference]
    if not judged_cases:
        print(f"[!!] no cases with a 'reference' field found in {dataset_path} -- nothing to judge")
        return 1

    cfg = load_config(args.config)
    retriever = build_retriever(cfg)
    answer_llm = build_answer_model(cfg)
    judge_llm, judge_label = build_judge_llm(cfg)
    print(f"[judge] answering with answer_model ({answer_llm.model}), judging with {judge_label}\n")

    k = args.k if args.k is not None else cfg["retriever"].get("answer_top_k", 5)
    max_expanded = cfg["retriever"].get("answer_max_expanded", 4)

    results = run_suite(cases, retriever, answer_llm, judge_llm, k, max_expanded)
    print_case_table(results)
    print_averages(results)

    errors = [r for r in results if r.status == "error"]
    if errors:
        print(f"\n[FAIL] {len(errors)} case(s) could not be judged (malformed judge output, twice)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
