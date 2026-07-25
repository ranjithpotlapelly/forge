"""Acceptance check: confirms num_ctx reaches Ollama and <think> reasoning
blocks (Qwen3) never reach the user, in both generate() and stream().

Requires the configured model (config.yaml llm.model) to actually be pulled
— run `ollama pull qwen3:8b` first if you see a "model not found" error.
Run from the repo root:  python -m app.test_reasoning
"""
from __future__ import annotations
import sys
from app.config_loader import load_config
from app.wiring import build_llm
from core.types import Message

_PROMPT = "What is 15% of 240? Think it through step by step, then give the final number."

def main() -> int:
    cfg = load_config()
    llm = build_llm(cfg)
    print(f"[ok] wired {type(llm).__name__} (model={llm.model}, num_ctx={llm.opts.get('num_ctx')})")

    if llm.opts.get("num_ctx") != 16384:
        print(f"[!!] expected num_ctx=16384 in options, got {llm.opts.get('num_ctx')!r}")
        return 1
    print("[ok] num_ctx=16384 is present in the options sent to Ollama")

    messages = [Message(role="user", content=_PROMPT)]

    print("\n--- stream() ---")
    streamed_parts: list[str] = []
    for piece in llm.stream(messages):
        streamed_parts.append(piece)
        print(piece, end="", flush=True)
    print()
    streamed = "".join(streamed_parts)
    if "<think>" in streamed or "</think>" in streamed:
        print("[!!] streamed answer still contains a <think> tag")
        return 1
    if not streamed.strip():
        print("[!!] streamed answer was empty")
        return 1
    print("[ok] streamed answer has no visible <think> tags")

    print("\n--- generate() ---")
    generated = llm.generate(messages)
    print(generated)
    if "<think>" in generated or "</think>" in generated:
        print("[!!] generate() answer still contains a <think> tag")
        return 1
    if not generated.strip():
        print("[!!] generate() answer was empty")
        return 1
    print("[ok] generate() answer has no visible <think> tags")

    print("\nReasoning check OK: num_ctx reaches Ollama, <think> blocks are stripped in both generate() and stream().")
    return 0

if __name__ == "__main__":
    sys.exit(main())
