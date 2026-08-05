"""Business logic (Prompt 27) for the code dependency graph: orchestrates
building it and answers "what calls X" / "what breaks if I change X" style
questions by traversing it. Vendor-free -- takes extract_fn and a
core.code_graph.CodeGraphStore as plain callables/ports, the same injection
pattern app/wiring.py already uses for plan_fn/apply_plan_fn, so this module
never imports tree_sitter or sqlite3 directly.
"""
from __future__ import annotations
import re
from typing import Any, Callable
from core.code_graph import CodeGraphStore, GraphNode

# Matches "what calls X", "who calls X", "callers of X", "what breaks if I
# change X" / "...if X changes" -- deliberately permissive (case-insensitive,
# optional trailing punctuation/parens) since this only has to catch the
# handful of natural phrasings that make sense for a graph query; anything
# else falls through to normal semantic Q&A.
_TARGET_RE = re.compile(
    r"(?:what\s+calls|who\s+calls|callers?\s+of|what\s+breaks\s+if\s+i\s+(?:change|modify|remove|delete)|"
    r"impact\s+of\s+changing)\s+([A-Za-z_][A-Za-z0-9_.]*)",
    re.IGNORECASE,
)

def is_graph_question(question: str) -> bool:
    return _TARGET_RE.search(question) is not None

def extract_target_symbol(question: str) -> str | None:
    """Pulls the symbol name out of a graph-shaped question, stripping a
    trailing call-parens/punctuation a user might have typed (e.g.
    "what calls loadUserByUsername()?" -> "loadUserByUsername")."""
    match = _TARGET_RE.search(question)
    if not match:
        return None
    symbol = match.group(1).rstrip(".,?!")
    symbol = re.sub(r"\(.*$", "", symbol)
    return symbol or None

def build_graph(extract_fn: Callable[[str], tuple[list, list]], graph_store: CodeGraphStore, repo_path: str) -> dict:
    """extract_fn is adapters.code_graph_extract.extract_repo, injected so this
    module never imports tree_sitter. Returns {"nodes": N, "edges": M}."""
    nodes, edges = extract_fn(repo_path)
    graph_store.add(nodes, edges)
    return {"nodes": len(nodes), "edges": len(edges)}

def _cite(node: GraphNode) -> str:
    return f"{node.path}:{node.start_line}-{node.end_line}"

def answer_graph_question(graph_store: CodeGraphStore, question: str) -> dict[str, Any] | None:
    """Returns None if `question` doesn't look like a graph question at all
    (caller should fall back to normal retrieval). Otherwise returns
    {"symbol": ..., "callers": [GraphNode, ...], "text": "<formatted answer>"}
    -- callers may be an empty list, which the text makes clear rather than
    reading like an error."""
    symbol = extract_target_symbol(question)
    if symbol is None:
        return None
    callers = graph_store.callers_of(symbol)
    if not callers:
        text = (
            f"No indexed caller of {symbol!r} was found in the dependency graph. "
            "This is name-based (no type resolution), so it's possible but not certain "
            "that nothing calls it -- or that the graph hasn't been built for this repo yet "
            "(see `python -m app.index_graph`)."
        )
    else:
        lines = "\n".join(f"- {c.symbol} — {_cite(c)}" for c in callers)
        text = (
            f"{len(callers)} caller(s) of {symbol!r} found in the dependency graph "
            f"(name-based match, not type-resolved):\n{lines}"
        )
    return {"symbol": symbol, "callers": callers, "text": text}
