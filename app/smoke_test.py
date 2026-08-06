"""Phase 1 smoke test: proves the environment boots.

Checks: (1) config.yaml loads and ${ENV} expansion works, (2) Ollama is
reachable, (3) the configured models are present. No heavy deps, no model calls.
Run from the repo root:  python -m app.smoke_test
"""
from __future__ import annotations
import sys
import json
import urllib.request
from app.config_loader import load_config

def check_ollama(host: str):
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=5) as r:
            data = json.loads(r.read())
        return True, [m["name"] for m in data.get("models", [])]
    except Exception as e:  # noqa: BLE001
        return False, [str(e)]

def main() -> int:
    cfg = load_config()
    print(f"[ok] config loaded: app = {cfg['app']['name']} ({cfg['app']['env']})")

    host = cfg["llm"]["host"] or "http://localhost:11434"
    ok, info = check_ollama(host)
    if not ok:
        print(f"[!!] Ollama not reachable at {host}: {info[0]}")
        print("     -> Install Ollama, run `ollama serve`, then retry.")
        return 1

    installed = list(info)
    print(f"[ok] Ollama reachable at {host} ({len(installed)} model(s) installed)")

    # llm/embeddings are always local. code_model may be hosted (Prompt 17's
    # OpenAI-compatible adapter, config.yaml's checked-in default) -- only
    # check it against local Ollama if it's actually configured as one;
    # "is it installed locally" doesn't apply to a hosted endpoint, and its
    # config shape differs too (models: a fallback list, not model: a string).
    wanted = {cfg["llm"]["model"], cfg["embeddings"]["model"]}
    code_model_adapter = cfg["code_model"]["adapter"]
    if code_model_adapter == "ollama":
        wanted.add(cfg["code_model"]["model"])
    else:
        hosted_models = [m for m in (cfg["code_model"].get("models") or []) if m] or [cfg["code_model"].get("model")]
        print(f"     [ok] code_model is hosted ({code_model_adapter}): {', '.join(filter(None, hosted_models)) or '(none configured)'}")

    all_present = True
    for m in sorted(wanted):
        base = m.split(":")[0]
        present = any(x.startswith(base) for x in installed)
        all_present = all_present and present
        if present:
            print(f"     [ok] {m}")
        else:
            print(f"     [..] {m}   -> run: ollama pull {m}")

    if all_present:
        print("\nPhase 1 OK. Next: Phase 2 - Reasoning (wire the Ollama adapter).")
    else:
        print("\nConfig + server OK. Pull the missing models above, then re-run.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
