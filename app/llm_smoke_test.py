"""Phase 2 smoke test: proves the Ollama adapter actually talks to the model.

Checks: (1) LLMClient.generate returns a real completion, (2) LLMClient.stream
yields incremental segments. Run from the repo root:  python -m app.llm_smoke_test
"""
from __future__ import annotations
import sys
from app.config_loader import load_config
from app.wiring import build_llm
from core.types import Message

def main() -> int:
    cfg = load_config()
    llm = build_llm(cfg)
    print(f"[ok] wired {type(llm).__name__} (model={llm.model}, host={llm.host})")

    prompt = [Message(role="user", content="Reply with exactly one word: pong")]

    try:
        text = llm.generate(prompt)
    except Exception as e:  # noqa: BLE001
        print(f"[!!] generate() failed: {e}")
        return 1
    print(f"[ok] generate() -> {text.strip()!r}")

    try:
        segments = list(llm.stream(prompt))
    except Exception as e:  # noqa: BLE001
        print(f"[!!] stream() failed: {e}")
        return 1
    streamed = "".join(segments)
    print(f"[ok] stream() -> {len(segments)} segment(s), joined = {streamed.strip()!r}")

    print("\nPhase 2 OK. Next: Phase 3 - Knowledge (code-aware RAG over Chroma).")
    return 0

if __name__ == "__main__":
    sys.exit(main())
