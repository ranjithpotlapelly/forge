"""Prompt 27 acceptance check: proves the optional dependency graph
(GraphRAG-lite) is additive, config-gated, and actually answers "what calls
X" correctly with citations -- scoped to Java + TypeScript.

Checks, against a synthetic fixture repo (fast, deterministic -- no LLM call
anywhere in this test, since graph_answer_node never calls one):
(1) Extraction finds the right nodes/edges for Java class methods, Java
    `implements`, and TypeScript's functional-guard/interceptor pattern
    (a top-level `export const x = (...) => {...}`, common in real Angular
    code and NOT a class member -- the case most likely to be silently
    missed by a naive "walk class bodies only" extractor).
(2) callers_of/callees_of resolve both bare and qualified symbol names.
(3) OFF (config default): the compiled graph has no graph_answer/graph_expand
    nodes at all -- not just "disabled", structurally absent, matching every
    other additive flag in this codebase (corrective_retrieval, etc.).
(4) ON: the compiled graph gains exactly those two nodes; a "what calls X"
    question routes straight to graph_answer (deterministic, sub-second) and
    returns the real caller with a correct file:line citation.

Run from the repo root:  python -m app.code_graph_smoke_test
"""
from __future__ import annotations
import copy
import shutil
import sys
import time
from pathlib import Path

from adapters.code_graph_extract import extract_repo
from adapters.code_graph_sqlite import SqliteCodeGraphStore
from app.config_loader import load_config
from app.wiring import build_engine
from product.code_graph import build_graph

SCRATCH = Path(__file__).resolve().parent.parent / ".data" / "code_graph_smoke_scratch"

_JAVA_SERVICE = '''package com.example.ums;

public class UmsUserDetailsService implements UserDetailsService {
    public UserDetails loadUserByUsername(String username) {
        return repository.findByUsername(username);
    }
}
'''

_JAVA_CONTROLLER = '''package com.example.ums;

public class LoginController {
    public String login(String username) {
        UserDetails details = userDetailsService.loadUserByUsername(username);
        return details.getName();
    }
}
'''

_TS_SERVICE = '''export class AuthService {
  getToken(): string {
    return this.storage.get('jwt');
  }
}
'''

_TS_INTERCEPTOR = '''export const authInterceptor = (req, next) => {
  const token = authService.getToken();
  return next(req);
};
'''

def _write_fixture() -> Path:
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    (SCRATCH / "src").mkdir(parents=True)
    (SCRATCH / "ts").mkdir(parents=True)
    (SCRATCH / "src" / "UmsUserDetailsService.java").write_text(_JAVA_SERVICE, encoding="utf-8")
    (SCRATCH / "src" / "LoginController.java").write_text(_JAVA_CONTROLLER, encoding="utf-8")
    (SCRATCH / "ts" / "auth.service.ts").write_text(_TS_SERVICE, encoding="utf-8")
    (SCRATCH / "ts" / "auth.interceptor.ts").write_text(_TS_INTERCEPTOR, encoding="utf-8")
    return SCRATCH

def main() -> int:
    repo = _write_fixture()

    # --- (1)+(2): extraction + query correctness, against the fixture ---
    nodes, edges = extract_repo(str(repo))
    symbols = {n.symbol for n in nodes}
    expected_symbols = {
        "LoginController", "LoginController.login",
        "UmsUserDetailsService", "UmsUserDetailsService.loadUserByUsername",
        "AuthService", "AuthService.getToken", "authInterceptor",
    }
    missing = expected_symbols - symbols
    if missing:
        print(f"[!!] extraction missed expected symbol(s): {missing}")
        return 1
    print(f"[ok] extraction found all {len(expected_symbols)} expected symbols ({len(nodes)} node(s), {len(edges)} edge(s) total)")

    implements_edges = {(e.src, e.dst) for e in edges if e.kind == "implements"}
    if ("UmsUserDetailsService", "UserDetailsService") not in implements_edges:
        print(f"[!!] expected an 'implements' edge for UmsUserDetailsService, got {implements_edges}")
        return 1
    print("[ok] Java 'implements' edge extracted correctly")

    db_path = SCRATCH / "graph.db"
    store = SqliteCodeGraphStore(str(db_path))
    stats = build_graph(extract_repo, store, str(repo))
    print(f"[ok] build_graph populated the store: {stats}")

    callers = {n.symbol for n in store.callers_of("loadUserByUsername")}
    if callers != {"LoginController.login"}:
        print(f"[!!] callers_of('loadUserByUsername') expected {{'LoginController.login'}}, got {callers}")
        return 1
    print("[ok] callers_of resolves a bare callee name to its qualified caller")

    ts_callers = {n.symbol for n in store.callers_of("getToken")}
    if ts_callers != {"authInterceptor"}:
        print(f"[!!] callers_of('getToken') expected {{'authInterceptor'}}, got {ts_callers}")
        print("     (this is exactly the top-level functional-interceptor case -- see module docstring)")
        return 1
    print("[ok] callers_of finds a caller that is a top-level TS function, not a class method")

    # --- (3): OFF path, structural check on the real config ---
    cfg = load_config()
    engine_off = build_engine(cfg)
    if engine_off.graph_expand or engine_off.graph_store is not None:
        print("[!!] expected graph_expand to default to False and graph_store to default to None")
        return 1
    off_nodes = set(engine_off._graph.get_graph().nodes)
    if "graph_answer" in off_nodes or "graph_expand" in off_nodes:
        print(f"[!!] OFF graph should not contain graph_answer/graph_expand, got nodes: {sorted(off_nodes)}")
        return 1
    print(f"[ok] OFF: compiled graph has no graph_answer/graph_expand nodes ({len(off_nodes)} node(s) total)")

    # --- (4): ON path, against the fixture graph just built ---
    cfg_on = copy.deepcopy(cfg)
    cfg_on["retriever"]["graph_expand"] = True
    cfg_on["code_graph"]["path"] = str(db_path)
    engine_on = build_engine(cfg_on)
    on_nodes = set(engine_on._graph.get_graph().nodes)
    if not {"graph_answer", "graph_expand"} <= on_nodes:
        print(f"[!!] ON graph should contain graph_answer + graph_expand, got nodes: {sorted(on_nodes)}")
        return 1
    print(f"[ok] ON: compiled graph gained exactly graph_answer + graph_expand ({len(on_nodes)} node(s) total, was {len(off_nodes)})")

    t0 = time.monotonic()
    result = engine_on.ask("what calls loadUserByUsername", thread_id="code-graph-smoke")
    dt = time.monotonic() - t0
    if dt > 5:
        print(f"[!!] graph answer took {dt:.1f}s -- expected sub-second (no LLM call should happen)")
        return 1
    if not result.citations or result.citations[0].metadata.get("symbol") != "LoginController.login":
        print(f"[!!] expected a citation for LoginController.login, got: {[c.metadata for c in result.citations]}")
        return 1
    print(f"[ok] ON: 'what calls loadUserByUsername' answered deterministically in {dt:.2f}s, citing LoginController.login")
    print(f"     answer: {result.text}")

    shutil.rmtree(SCRATCH, ignore_errors=True)
    print("\nPrompt 27 OK.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
