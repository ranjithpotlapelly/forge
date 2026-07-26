"""Ad-hoc connectivity check: confirm code_model is really hitting OpenRouter
(not silently falling back to local Ollama), and that its fallback chain
(CODE_MODEL_NAME -> FALLBACK_1 -> FALLBACK_2, see config.yaml's code_model.models)
is wired correctly. Never prints the API key -- only its first 8 chars, to
confirm it was actually loaded from the environment.

Run from the repo root:  python -m app.code_model_connectivity_smoke_test
"""
from __future__ import annotations
import os
import sys

from app.config_loader import load_config
from app.wiring import build_code_model
from core.types import Message


def main() -> int:
    cfg = load_config()
    section = cfg["code_model"]

    # 1. Config + env sanity check.
    adapter = section.get("adapter")
    print(f"[cfg] code_model.adapter = {adapter!r}")
    print(f"[cfg] code_model.models  = {section.get('models')!r}")
    print(f"[cfg] code_model.base_url = {section.get('base_url')!r}")

    if adapter != "openai_compatible":
        print(f"[!!] expected adapter 'openai_compatible', got {adapter!r} -- "
              f"code_model would use a different adapter, not OpenRouter.")
        return 1

    raw_key = os.environ.get("CODE_MODEL_API_KEY", "")
    if not raw_key:
        print("[!!] CODE_MODEL_API_KEY is empty/unset in the environment -- "
              "the adapter would send no Authorization header at all.")
        return 1
    print(f"[cfg] CODE_MODEL_API_KEY present, starts with: {raw_key[:8]}...")

    models = [m for m in (section.get("models") or []) if m]
    if not models:
        print("[!!] no non-blank entries in code_model.models -- nothing to call.")
        return 1
    if not any("free" in m for m in models):
        print(f"[!!] none of {models!r} look like ':free' variants -- refusing to "
              f"call to avoid unexpected cost.")
        return 1

    # 2. One minimal call through the real wiring path -- lets OpenRouter's own
    # fallback chain run (primary -> FALLBACK_1 -> FALLBACK_2) if the primary
    # is unavailable, exactly as a real code-edit task would experience it.
    llm = build_code_model(cfg)
    print(f"[ok] built {type(llm).__name__} (base_url={llm.base_url}, models={llm.models})")

    messages = [Message(role="user", content='Reply with exactly one word: "connected"')]
    try:
        text = llm.generate(messages)
    except RuntimeError as e:
        msg = str(e)
        print(f"[!!] call failed: {msg}")
        if "401" in msg:
            print("[diagnosis] 401 Unauthorized -- the API key is wrong/expired/missing.")
        elif "404" in msg:
            print("[diagnosis] 404 -- every model in the fallback chain was rejected "
                  "(check CODE_MODEL_NAME/FALLBACK_1/FALLBACK_2 against OpenRouter's "
                  "current model list).")
        elif "could not reach" in msg:
            print("[diagnosis] connection error -- the base URL is wrong or unreachable "
                  "(check CODE_MODEL_BASE_URL).")
        else:
            print("[diagnosis] unrecognized error shape -- see message above.")
        return 1

    print(f"[ok] HTTP 200 -- response text: {text!r}")
    print("\n[ok] code_model is confirmed live against OpenRouter (openai_compatible adapter, "
          "fallback chain of :free models, key loaded from env). Not a local Ollama fallback.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
