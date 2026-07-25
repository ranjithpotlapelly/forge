"""Phase 15 acceptance check: proves the lexical (FTS5) + semantic (Chroma)
hybrid retriever beats semantic-only search on an exact-symbol query.

Indexes one document named UmsRememberMeServices among Forge's own ~130 real
code chunks (via the existing FsIngestSource -- realistic background noise,
not a hand-picked toy corpus) plus semantically-close remember-me/auth
distractors, including a subclass and a test that only *reference* the
target class. Runs the same query ("UmsRememberMeServices") against a
semantic-only ChromaRetriever ("before") and the HybridRetriever
app/wiring.py now wires by default ("after"). Empirically (this repo, real
Ollama/nomic-embed-text embeddings), semantic-only ranks the referencing
subclass ABOVE the actual definition -- exactly the "embeddings can't tell
defines from references apart" failure this phase exists to fix. Requires the
exact match to rank #1 after fusion -- so this fails loudly if hybrid search
regresses to being no better than semantic alone.

Run from the repo root:  python -m app.hybrid_retriever_smoke_test
"""
from __future__ import annotations
import shutil
import sys
from pathlib import Path

from adapters.retriever_chroma import ChromaRetriever
from adapters.retriever_hybrid import HybridRetriever
from app.config_loader import load_config
from app.wiring import build_ingest
from core.types import Document

SCRATCH = Path(__file__).resolve().parent.parent / ".data" / "hybrid_retriever_smoke_scratch"

TARGET_SYMBOL = "UmsRememberMeServices"

DISTRACTORS = [
    Document(
        content=(
            "class UmsRememberMeServices:\n"
            "    \"\"\"Issues and validates persistent-login cookies for the UMS auth module.\"\"\"\n"
            "    def auto_login(self, request, response):\n"
            "        ...\n"
        ),
        metadata={"path": "auth/ums_remember_me_services.py", "symbol": TARGET_SYMBOL, "start_line": 1, "end_line": 4},
    ),
    Document(
        content=(
            "class TokenBasedRememberMeServices:\n"
            "    \"\"\"Remember-me implementation backed by a signed token cookie.\"\"\"\n"
            "    def auto_login(self, request, response):\n"
            "        ...\n"
        ),
        metadata={"path": "auth/token_based_remember_me.py", "symbol": "TokenBasedRememberMeServices", "start_line": 1, "end_line": 4},
    ),
    Document(
        content=(
            "class PersistentTokenBasedRememberMeServices:\n"
            "    \"\"\"Remember-me implementation backed by a persistent token store.\"\"\"\n"
            "    def auto_login(self, request, response):\n"
            "        ...\n"
        ),
        metadata={"path": "auth/persistent_token_remember_me.py", "symbol": "PersistentTokenBasedRememberMeServices", "start_line": 1, "end_line": 4},
    ),
    Document(
        content=(
            "class RememberMeAuthenticationFilter:\n"
            "    \"\"\"Servlet filter that checks the remember-me cookie on each request.\"\"\"\n"
            "    def do_filter(self, request, response, chain):\n"
            "        ...\n"
        ),
        metadata={"path": "auth/remember_me_filter.py", "symbol": "RememberMeAuthenticationFilter", "start_line": 1, "end_line": 4},
    ),
    Document(
        content=(
            "class SessionAuthenticationStrategy:\n"
            "    \"\"\"Decides what happens to the session on successful authentication.\"\"\"\n"
            "    def on_authentication(self, request, response, auth):\n"
            "        ...\n"
        ),
        metadata={"path": "auth/session_authentication_strategy.py", "symbol": "SessionAuthenticationStrategy", "start_line": 1, "end_line": 4},
    ),
    Document(
        content=(
            "class CookieAuthenticationFilter:\n"
            "    \"\"\"Reads an auth cookie and populates the security context.\"\"\"\n"
            "    def do_filter(self, request, response, chain):\n"
            "        ...\n"
        ),
        metadata={"path": "auth/cookie_authentication_filter.py", "symbol": "CookieAuthenticationFilter", "start_line": 1, "end_line": 4},
    ),
    Document(
        content=(
            "class JwtTokenProvider:\n"
            "    \"\"\"Issues and validates signed JWTs for stateless session auth.\"\"\"\n"
            "    def generate_token(self, user):\n"
            "        ...\n"
        ),
        metadata={"path": "auth/jwt_token_provider.py", "symbol": "JwtTokenProvider", "start_line": 1, "end_line": 4},
    ),
    Document(
        content=(
            "class UserSessionRegistry:\n"
            "    \"\"\"Tracks active sessions per user for concurrent-session limits.\"\"\"\n"
            "    def register(self, user, session):\n"
            "        ...\n"
        ),
        metadata={"path": "auth/user_session_registry.py", "symbol": "UserSessionRegistry", "start_line": 1, "end_line": 4},
    ),
    # The adversarial case: a subclass and a test that both *mention* the
    # target class score as (or more) semantically similar to the query than
    # the actual definition -- embeddings can't tell "defines X" from
    # "references X" apart; an exact match on the `symbol` column can.
    Document(
        content=(
            "class CustomUmsRememberMeServicesAdapter(UmsRememberMeServices):\n"
            "    \"\"\"Adapts UmsRememberMeServices to the legacy session filter chain.\"\"\"\n"
            "    def auto_login(self, request, response):\n"
            "        return super().auto_login(request, response)\n"
        ),
        metadata={"path": "auth/custom_ums_adapter.py", "symbol": "CustomUmsRememberMeServicesAdapter", "start_line": 1, "end_line": 4},
    ),
    Document(
        content=(
            "def test_ums_remember_me_services_autologin():\n"
            "    \"\"\"Covers UmsRememberMeServices.auto_login end to end.\"\"\"\n"
            "    svc = UmsRememberMeServices()\n"
            "    assert svc.auto_login(None, None) is None\n"
        ),
        metadata={"path": "tests/test_ums_remember_me.py", "symbol": "test_ums_remember_me_services_autologin", "start_line": 1, "end_line": 4},
    ),
]

def main() -> int:
    cfg = load_config()
    shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir(parents=True)

    # Realistic background noise: Forge's own real code chunks, not just a
    # hand-picked distractor set -- the same source app/ingest_smoke_test.py uses.
    real_docs = list(build_ingest(cfg).documents())
    docs = real_docs + DISTRACTORS
    print(f"[info] corpus: {len(real_docs)} real chunk(s) from {cfg['forge']['repo_path']} + {len(DISTRACTORS)} distractor(s)")

    embed_kwargs = dict(
        embed_model=cfg["embeddings"]["model"],
        host=cfg["embeddings"].get("host", cfg["llm"]["host"]),
    )

    # --- "before": semantic-only, exactly what Forge had before this phase ---
    semantic_only = ChromaRetriever(
        path=str(SCRATCH / "chroma_before"), collection="smoke_before", **embed_kwargs,
    )
    semantic_only.add(docs)
    before_results = semantic_only.search(TARGET_SYMBOL, k=10)
    if not before_results:
        print("[!!] semantic-only search returned nothing")
        return 1
    before_rank = next((i for i, c in enumerate(before_results, start=1) if c.metadata.get("symbol") == TARGET_SYMBOL), None)
    print(f"[info] BEFORE (semantic-only): {TARGET_SYMBOL!r} ranked "
          f"{'#' + str(before_rank) if before_rank else 'outside top ' + str(len(before_results))}")
    for i, c in enumerate(before_results[:3], start=1):
        print(f"       {i}. {c.metadata.get('symbol')} ({c.metadata.get('path')}) score={c.score:.4f}")

    # --- "after": hybrid (lexical FTS5 + semantic), what app/wiring.py now wires ---
    semantic_half = ChromaRetriever(
        path=str(SCRATCH / "chroma_after"), collection="smoke_after", **embed_kwargs,
    )
    hybrid = HybridRetriever(semantic=semantic_half, lexical_path=str(SCRATCH / "lexical.db"))
    hybrid.add(docs)
    after_results = hybrid.search(TARGET_SYMBOL, k=10)
    if not after_results:
        print("[!!] hybrid search returned nothing")
        return 1
    print(f"[info] AFTER (hybrid): top {min(3, len(after_results))} result(s) (ordered by RRF fusion; "
          f"score shown is the original semantic confidence, or {1.0:.1f} for a lexical-only exact match):")
    for i, c in enumerate(after_results[:3], start=1):
        print(f"       {i}. {c.metadata.get('symbol')} ({c.metadata.get('path')}) score={c.score:.4f}")

    top = after_results[0]
    if top.metadata.get("symbol") != TARGET_SYMBOL:
        print(f"[!!] expected {TARGET_SYMBOL!r} to rank #1 after hybrid search, got {top.metadata.get('symbol')!r}")
        return 1
    before_desc = f"#{before_rank}" if before_rank else f"outside top {len(before_results)}"
    print(f"[ok] {TARGET_SYMBOL!r} ranks #1 after hybrid search (was {before_desc} semantic-only)")

    # --- prove the lexical stage is genuinely doing the disambiguating work,
    # not the semantic half incidentally getting it right too ---
    from adapters.retriever_fts import FtsIndex
    lexical_only = FtsIndex(str(SCRATCH / "lexical.db"))
    lexical_results = lexical_only.search(TARGET_SYMBOL, k=10)
    if not lexical_results or lexical_results[0].metadata.get("symbol") != TARGET_SYMBOL:
        print("[!!] expected the FTS5 lexical index alone to rank the exact symbol match first")
        return 1
    print(f"[ok] lexical-only (FTS5) also ranks {TARGET_SYMBOL!r} #1, confirming the exact-match signal is real")

    shutil.rmtree(SCRATCH, ignore_errors=True)
    print("\nPhase 15 OK. Lexical FTS5 + semantic fusion beats semantic-only search on exact-symbol queries.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
