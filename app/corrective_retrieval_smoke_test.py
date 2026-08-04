"""Prompt 25 acceptance check: proves the corrective-retrieval (self-RAG/CRAG)
loop added to the Q&A answer path is additive and config-gated.

Checks:
(1) OFF (config.yaml's answer.corrective_retrieval default, unchanged): a
    normal question answers exactly as before -- cited, no correction fired.
(2) ON: a deliberately vague question ("how does the app decide which page
    to show for a given url") whose first retrieval (all 8 results score
    below MIN_RELEVANCE, topping out around 0.07 -- app.routes.ts, the file
    that actually answers this, isn't even in the top-8) would decline
    outright without correction. The grading step should flag it
    insufficient, run ONE corrective retrieval with a refined query, and the
    merged/re-ranked results should surface app.routes.ts with a real answer
    instead of a decline.
(3) The correction can never fire more than once, structurally: with the flag
    on, corrective_retrieve always routes onward to answer/decline, never
    back to grade_retrieval (see adapters/engine_langgraph.py's _build()).

Run from the repo root:  python -m app.corrective_retrieval_smoke_test
(Two real Ollama generate() calls happen in the ON branch -- grade + answer,
each several minutes on CPU. Expect this to take a while.)
"""
from __future__ import annotations
import copy
import io
import sys
import time
from contextlib import redirect_stdout

from app.config_loader import load_config
from app.wiring import build_engine

OFF_QUESTION = "how does the app authenticate a user and store the auth token"
ON_QUESTION = "how does the app decide which page to show for a given url"

def main() -> int:
    cfg = load_config()

    # --- OFF: default config, behaviour identical to before this feature ---
    engine_off = build_engine(cfg)
    if engine_off.corrective_retrieval:
        print("[!!] expected corrective_retrieval to default to False")
        return 1

    buf = io.StringIO()
    t0 = time.monotonic()
    with redirect_stdout(buf):
        result_off = engine_off.ask(OFF_QUESTION, thread_id="corrective-smoke-off")
    dt_off = time.monotonic() - t0
    captured_off = buf.getvalue()
    sys.stdout.write(captured_off)
    print(f"[timing] OFF total {dt_off:.1f}s")

    if not result_off.text.strip() or not result_off.citations:
        print("[!!] OFF: expected a normal cited answer")
        return 1
    if "[corrective-retrieval]" in captured_off:
        print("[!!] OFF: correction logic fired even though the flag is off")
        return 1
    print(f"[ok] OFF: normal answer ({len(result_off.citations)} citation(s)), no corrective-retrieval log lines")

    # --- ON: same repo, flag flipped, a vague question ---
    cfg_on = copy.deepcopy(cfg)
    cfg_on.setdefault("answer", {})["corrective_retrieval"] = True
    engine_on = build_engine(cfg_on)
    if not engine_on.corrective_retrieval:
        print("[!!] expected corrective_retrieval to be True on the ON engine")
        return 1

    buf = io.StringIO()
    t1 = time.monotonic()
    with redirect_stdout(buf):
        result_on = engine_on.ask(ON_QUESTION, thread_id="corrective-smoke-on")
    dt_on = time.monotonic() - t1
    captured_on = buf.getvalue()
    sys.stdout.write(captured_on)
    print(f"[timing] ON total {dt_on:.1f}s")

    fired = captured_on.count("[corrective-retrieval] correction fired")
    if fired == 0:
        print("[!!] ON: expected a correction to fire for this deliberately vague question")
        print("     (retrieval quality can drift with re-indexing -- pick a new vague question if this keeps failing)")
        return 1
    if fired > 1:
        print(f"[!!] ON: correction fired {fired} times -- the one-correction cap is broken")
        return 1
    print(f"[ok] ON: correction fired exactly once, with a refined query logged above")

    if not result_on.citations or "I don't have enough indexed context" in result_on.text:
        print("[!!] ON: expected the correction to rescue this from a decline into a real cited answer")
        return 1
    paths = {c.metadata.get("path") for c in result_on.citations}
    if not any("app.routes.ts" in (p or "") for p in paths):
        print(f"[!!] ON: expected app.routes.ts among the final citations, got {sorted(p for p in paths if p)}")
        return 1
    print(f"[ok] ON: final answer is cited (not a decline) and includes app.routes.ts -- the file the vague first pass missed entirely")

    print("\nPrompt 25 OK.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
