"""Adapter (Phase 15): a SQLite FTS5 lexical index over symbol names,
signatures, and file paths.

Embeddings are weak at exact identifier matching ("where is validateToken
called?" needs the literal token, not its vibe). This index exists to answer
that half of the query well; it's not a general-purpose retriever on its own
-- adapters/retriever_hybrid.py fuses it with the semantic (Chroma) one
behind core.retriever.Retriever, and is the only thing that imports this
module.
"""
from __future__ import annotations
import json
import re
import sqlite3
from pathlib import Path
from typing import Iterable

from adapters._doc_id import document_id
from core.types import Chunk, Document

_WORD_RE = re.compile(r"\w+")

# unicode61 (FTS5's default tokenizer) splits identifiers on underscore --
# snake_case names like "approval_gate_node" or "run_tool" tokenize into
# "approval"/"gate"/"node" and "run"/"tool", not one token (camelCase/PascalCase
# survive as one token, since case alone isn't a separator). Left unfiltered,
# ordinary English words in a natural-language query ("how", "for", "a", ...)
# spuriously match those sub-tokens in unrelated snake_case symbols, drowning
# out the exact-identifier signal this index exists for.
_STOPWORDS = frozenset("""
    a an the this that these those is are was were be been being do does did
    doing how what when where why who whom which whose of in on for to with
    and or but not no nor it its as by at from into onto about if then than
    so there here can could should would will shall may might must i you he
    she we they them his her our your their my mine yours ours theirs
""".split())

# Column weights for bm25(), positional to the table's column order below.
# Symbol matches an exact identifier lookup cares about most; signature (the
# def/class header line) next; file path last -- content/id/metadata are
# UNINDEXED so no term ever matches there and their weight is moot.
_BM25_WEIGHTS = (0.0, 5.0, 2.0, 1.0, 0.0, 0.0)

_PATH_SEP_RE = re.compile(r"[/\\._-]")

def _signature(content: str) -> str:
    """The chunk's def/class header: its first non-blank line."""
    for line in content.splitlines():
        if line.strip():
            return line.strip()
    return ""

def _path_words(path: str) -> str:
    """Split a file path into space-separated words for indexing.

    Identifiers stay atomic (tokenchars '_' below), but a path is a sequence
    of directory/file *words*, not one identifier -- "adapters/mcp_servers/
    workspace_server.py" should be findable by "workspace" or "mcp" on their
    own. Splitting it into words here, before it reaches FTS5, keeps that
    true independent of how the table tokenizes underscores everywhere else.
    """
    return " ".join(w for w in _PATH_SEP_RE.split(path) if w)

def _match_query(text: str) -> str | None:
    """Free text -> a safe FTS5 MATCH expression, or None if nothing but
    stopwords survives (a purely natural-language query with no identifier-
    shaped term in it -- correct behavior is to contribute zero lexical
    candidates and let semantic search carry it, not to match noise).

    Terms are double-quoted (so punctuation/FTS keywords like NOT/OR/AND in
    the raw query can never break MATCH syntax) and OR-joined (so a
    multi-word natural-language question still matches rows containing just
    one of its terms, instead of implicit AND emptying the result set).
    """
    terms = [t for t in _WORD_RE.findall(text) if t.lower() not in _STOPWORDS]
    if not terms:
        return None
    return " OR ".join(f'"{t}"' for t in terms)

class FtsIndex:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS lexical USING fts5("
            "id UNINDEXED, symbol, signature, path, content UNINDEXED, metadata UNINDEXED, "
            # unicode61's default separators split snake_case (approval_gate_node
            # -> approval/gate/node), which would let any one of those common
            # English words match on their own. Adding '_' as a token character
            # keeps identifiers atomic, so a match means "this whole identifier",
            # not "one word that happens to also appear in a longer name".
            "tokenize=\"unicode61 tokenchars '_'\")"
        )
        self._conn.commit()

    def add(self, documents: Iterable[Document]) -> None:
        rows = [
            (
                document_id(doc),
                str((doc.metadata or {}).get("symbol", "")),
                _signature(doc.content),
                _path_words(str((doc.metadata or {}).get("path", ""))),
                doc.content,
                json.dumps(doc.metadata or {}),
            )
            for doc in documents
        ]
        if not rows:
            return
        # FTS5 has no unique constraint to upsert against, so re-indexing the
        # same document (a repeat ingest run) would otherwise duplicate rows
        # -- delete any existing row for each id first, matching Chroma's
        # upsert-by-id semantics (both use adapters._doc_id.document_id).
        self._conn.executemany("DELETE FROM lexical WHERE id = ?", [(r[0],) for r in rows])
        self._conn.executemany(
            "INSERT INTO lexical (id, symbol, signature, path, content, metadata) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()

    def search(self, query: str, k: int = 8) -> list[Chunk]:
        match = _match_query(query)
        if match is None:
            return []
        weights = ", ".join(str(w) for w in _BM25_WEIGHTS)
        rows = self._conn.execute(
            f"SELECT content, metadata, bm25(lexical, {weights}) AS rank "
            "FROM lexical WHERE lexical MATCH ? ORDER BY rank LIMIT ?",
            (match, k),
        ).fetchall()
        # bm25() is non-positive; smaller (more negative) = better match, so
        # negate into the higher-is-better convention core.types.Chunk uses.
        return [Chunk(content=content, metadata=json.loads(metadata), score=-rank) for content, metadata, rank in rows]
