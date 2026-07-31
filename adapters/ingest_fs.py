"""Adapter (Phase 10, extended later): implements core.ingest.IngestSource by
walking a local repo checkout and yielding structure-aware chunks.

Python files are chunked per top-level function/class via the stdlib `ast`
module (no tree-sitter for Python: Phase 9 dropped it as an unused
dependency, and ast already gives real function/class-level chunks for
Python). JavaScript/TypeScript/Java are chunked per method via tree-sitter
(_ts_chunks/_GRAMMAR_BY_EXT below) -- tree-sitter came back for these because
the project's actual daily-driver target repo is Angular/TypeScript and a
Java repo is indexed periodically too, and whole-file-only chunking there
meant product/code_edit.py's edit step had to regenerate an entire file
(often ~one class per file in both languages) to change one method. Go isn't
an active target repo, so it stays on whole-file chunking rather than adding
a grammar for it. Anything else configured falls back to whole-file chunks,
since there's no parser for it here.
"""
from __future__ import annotations
import ast
import subprocess
from pathlib import Path
from typing import Iterable
from tree_sitter import Language, Node, Parser
import tree_sitter_java as tsjava
import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
from core.types import Document

_EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".data", ".idea", ".claude",
    ".chainlit", "node_modules", ".pytest_cache", "dist", "build", "target",
}

_LANGUAGE_EXTENSIONS = {
    "python": (".py",),
    "javascript": (".js", ".jsx"),
    "typescript": (".ts", ".tsx"),
    "go": (".go",),
    "java": (".java",),
}

class _GrammarSpec:
    """One tree-sitter grammar plus which node types count as a chunkable
    symbol for that language. Keyed by file EXTENSION (not language name,
    like _LANGUAGE_EXTENSIONS is) because .ts and .tsx need different
    sub-grammars despite both being "typescript"."""
    def __init__(
        self, language: Language, containers: set[str], members: set[str],
        top_level: set[str], lang_tag: str,
    ):
        self.language = language
        self.containers = containers  # class/interface-like -- body-walked for members
        self.members = members        # method/constructor types found inside a container's body
        self.top_level = top_level    # leaf types extracted directly at module level
        self.lang_tag = lang_tag      # value stored in metadata["language"]

# Java's top_level is empty -- everything in Java lives inside a
# class/interface/enum, there's no bare top-level function.
_GRAMMAR_BY_EXT: dict[str, _GrammarSpec] = {
    ".js": _GrammarSpec(
        Language(tsjs.language()), {"class_declaration"}, {"method_definition"},
        {"function_declaration"}, "javascript",
    ),
    ".jsx": _GrammarSpec(
        Language(tsjs.language()), {"class_declaration"}, {"method_definition"},
        {"function_declaration"}, "javascript",
    ),
    ".ts": _GrammarSpec(
        Language(tsts.language_typescript()), {"class_declaration", "interface_declaration"},
        {"method_definition", "method_signature"}, {"function_declaration"}, "typescript",
    ),
    ".tsx": _GrammarSpec(
        Language(tsts.language_tsx()), {"class_declaration", "interface_declaration"},
        {"method_definition", "method_signature"}, {"function_declaration"}, "typescript",
    ),
    ".java": _GrammarSpec(
        Language(tsjava.language()), {"class_declaration", "interface_declaration", "enum_declaration"},
        {"method_declaration", "constructor_declaration"}, set(), "java",
    ),
}

def _ts_span(node: Node) -> tuple[int, int]:
    """1-indexed (start_line, end_line), matching _python_chunks' convention
    -- tree-sitter's own points are 0-indexed (row, col)."""
    return node.start_point[0] + 1, node.end_point[0] + 1

def _ts_span_with_leading_comment(siblings, index: int) -> tuple[int, int]:
    """Like _ts_span, but extends start_line backward over any immediately
    preceding `comment` node(s) -- a Javadoc/JSDoc/godoc comment sits as a
    separate sibling directly above a declaration, not inside it (unlike a
    Python docstring, which IS the function body's first statement and so is
    already part of _python_chunks' span for free). Without this, chunking
    per-symbol silently drops exactly the kind of high-signal text
    (`/** Attaches the JWT to every request... */`) that made whole-file
    chunks retrieve correctly before -- caught by a real search regression
    against auth.interceptor.ts during verification. Allows up to one blank
    line between a trailing comment and the node it documents."""
    node = siblings[index]
    start_row = node.start_point[0]
    i = index
    while i > 0 and siblings[i - 1].type == "comment" and start_row - siblings[i - 1].end_point[0] <= 2:
        i -= 1
        start_row = siblings[i].start_point[0]
    return start_row + 1, node.end_point[0] + 1

def _ts_declaration(node: Node) -> Node:
    """JS/TS wrap every exported class/interface/function in an
    export_statement node (`export class Foo {}` -> export_statement whose
    "declaration" field is the class_declaration) -- real-world Angular/TS
    code is almost entirely `export class Foo { ... }`, so without unwrapping
    this, node.type would never match a container/top_level entry and
    nothing would ever get chunked. Java has no such wrapper (no ES
    modules), so this is a no-op for it. `export default class Foo {}` and
    bare re-exports (`export { Foo }`, no "declaration" field) are handled
    too -- the latter falls back to returning the export_statement itself,
    which won't match anything and is correctly skipped."""
    if node.type == "export_statement":
        decl = node.child_by_field_name("declaration")
        if decl is not None:
            return decl
    return node

def _ts_name(node: Node) -> str | None:
    """The one shared name-extraction call for containers/members/top-level
    nodes alike, across every grammar here. Deliberately NOT "scan for the
    first identifier child": in Java (and same issue in TS) the return type
    comes before the method name, e.g. `public UserDetails
    loadUserByUsername(...)` -- a naive first-identifier scan would misname
    the symbol as the return type. child_by_field_name("name") sidesteps
    that unconditionally, no per-language special-casing needed."""
    name_node = node.child_by_field_name("name")
    return name_node.text.decode("utf-8") if name_node is not None else None

class FsIngestSource:
    def __init__(self, repo_path: str, languages: list[str]):
        self.repo_path = Path(repo_path).resolve()
        self.languages = languages
        self._extensions = {
            ext for lang in languages for ext in _LANGUAGE_EXTENSIONS.get(lang, ())
        }

    def documents(self, only: list[str] | None = None) -> Iterable[Document]:
        """only, if given, is a list of paths relative to repo_path (e.g. from
        changed_paths()) -- skips the full rglob walk and reads just those
        files, for reindexing what changed instead of the whole tree. Not
        part of the IngestSource port (index_repo never passes it); additive,
        used only by product.indexing.index_changed_files."""
        for path in self._walk(only):
            if path.suffix == ".py":
                yield from self._python_chunks(path)
            elif path.suffix in _GRAMMAR_BY_EXT:
                yield from self._ts_chunks(path, _GRAMMAR_BY_EXT[path.suffix])
            else:
                yield from self._whole_file_chunk(path)

    def _walk(self, only: list[str] | None = None) -> Iterable[Path]:
        if only is not None:
            for rel in only:
                path = self.repo_path / rel
                if path.is_file() and path.suffix in self._extensions:
                    yield path
            return
        for path in self.repo_path.rglob("*"):
            if not path.is_file() or path.suffix not in self._extensions:
                continue
            if _EXCLUDED_DIRS & set(path.relative_to(self.repo_path).parts[:-1]):
                continue
            yield path

    def _rel(self, path: Path) -> str:
        return str(path.relative_to(self.repo_path)).replace("\\", "/")

    def _python_chunks(self, path: Path) -> Iterable[Document]:
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return
        if not source.strip():
            return
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            return

        lines = source.splitlines()
        rel_path = self._rel(path)
        chunked = False
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start, end = node.lineno, getattr(node, "end_lineno", node.lineno)
                yield Document(
                    content="\n".join(lines[start - 1:end]),
                    metadata={
                        "path": rel_path, "symbol": node.name,
                        "start_line": start, "end_line": end, "language": "python",
                    },
                )
                chunked = True
        if not chunked:
            yield Document(
                content=source,
                metadata={
                    "path": rel_path, "symbol": "<module>",
                    "start_line": 1, "end_line": len(lines), "language": "python",
                },
            )

    def _whole_file_chunk(self, path: Path, language: str | None = None) -> Iterable[Document]:
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return
        if not source.strip():
            return
        yield Document(
            content=source,
            metadata={
                "path": self._rel(path), "symbol": "<file>",
                "start_line": 1, "end_line": len(source.splitlines()),
                "language": language,
            },
        )

    def _ts_chunks(self, path: Path, spec: _GrammarSpec) -> Iterable[Document]:
        """Per-method/function chunks via tree-sitter, for every language in
        _GRAMMAR_BY_EXT. Mirrors _python_chunks' shape (chunked flag +
        whole-file fallback) but additionally walks one level into
        class/interface bodies (spec.containers -> spec.members), since a
        typical Angular/TS component or Java class puts nearly all its code
        inside one class per file -- chunking only at class granularity would
        collapse right back to whole-file size for exactly the languages this
        exists for."""
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return
        if not source.strip():
            return

        parser = Parser(spec.language)
        tree = parser.parse(source.encode("utf-8"))
        if tree.root_node.has_error:
            # tree-sitter is error-tolerant and won't raise on malformed
            # input -- it returns a best-effort tree with ERROR nodes rather
            # than failing, so this has to be an explicit check (unlike
            # _python_chunks' except SyntaxError), not a try/except.
            yield from self._whole_file_chunk(path, language=spec.lang_tag)
            return

        lines = source.splitlines()
        rel_path = self._rel(path)
        chunked = False
        top_children = tree.root_node.children

        for i, node in enumerate(top_children):
            # Use the unwrapped declaration to classify (export_statement's
            # own type never matches a container/top_level entry), but keep
            # the OUTER node's span for whatever gets extracted at this
            # level -- export_statement's span starts at "export" (and
            # includes any decorator immediately above it, e.g. Angular's
            # @Component(...)), so content extracted this way still includes
            # both. Member extraction inside a class body is unaffected --
            # class bodies don't wrap their methods in export_statement.
            decl = _ts_declaration(node)
            if decl.type in spec.containers:
                class_name = _ts_name(decl) or "<anonymous>"
                body = decl.child_by_field_name("body")
                body_children = body.children if body else []
                member_idxs = [j for j, c in enumerate(body_children) if c.type in spec.members]
                if member_idxs:
                    for j in member_idxs:
                        member_name = _ts_name(body_children[j])
                        if member_name is None:
                            continue
                        start, end = _ts_span_with_leading_comment(body_children, j)
                        yield Document(
                            content="\n".join(lines[start - 1:end]),
                            metadata={
                                "path": rel_path, "symbol": f"{class_name}.{member_name}",
                                "start_line": start, "end_line": end, "language": spec.lang_tag,
                            },
                        )
                        chunked = True
                else:
                    # No matched member nodes (e.g. a class with only
                    # arrow-function-valued properties, which parse as
                    # public_field_definition, not method_definition) -- one
                    # whole-class chunk instead of silently indexing nothing,
                    # mirroring _python_chunks' `chunked` fallback.
                    start, end = _ts_span_with_leading_comment(top_children, i)
                    yield Document(
                        content="\n".join(lines[start - 1:end]),
                        metadata={
                            "path": rel_path, "symbol": class_name,
                            "start_line": start, "end_line": end, "language": spec.lang_tag,
                        },
                    )
                    chunked = True
            elif decl.type in spec.top_level:
                name = _ts_name(decl)
                if name is None:
                    continue
                start, end = _ts_span_with_leading_comment(top_children, i)
                yield Document(
                    content="\n".join(lines[start - 1:end]),
                    metadata={
                        "path": rel_path, "symbol": name,
                        "start_line": start, "end_line": end, "language": spec.lang_tag,
                    },
                )
                chunked = True
            elif decl.type in ("lexical_declaration", "variable_declaration"):
                # `export const authGuard: CanActivateFn = (route, state) =>
                # {...}` -- a very common modern Angular pattern (functional
                # guards/interceptors/resolvers replaced class-based ones),
                # parses as lexical_declaration -> variable_declarator, not
                # function_declaration, so spec.top_level alone misses it.
                # Only treat a SINGLE declarator bound directly to a function
                # value as a leaf -- `export const A = 1, B = 2;` or a plain
                # data constant must still fall through untouched.
                declarators = [c for c in decl.children if c.type == "variable_declarator"]
                if len(declarators) == 1:
                    value = declarators[0].child_by_field_name("value")
                    if value is not None and value.type in ("arrow_function", "function_expression"):
                        name = _ts_name(declarators[0])
                        if name is not None:
                            start, end = _ts_span_with_leading_comment(top_children, i)
                            yield Document(
                                content="\n".join(lines[start - 1:end]),
                                metadata={
                                    "path": rel_path, "symbol": name,
                                    "start_line": start, "end_line": end, "language": spec.lang_tag,
                                },
                            )
                            chunked = True

        if not chunked:
            yield from self._whole_file_chunk(path, language=spec.lang_tag)

def changed_paths(repo_path: str) -> tuple[list[str], list[str]]:
    """Working-tree file paths (relative to repo_path, forward-slashed) that
    differ from HEAD, split into (changed, deleted) -- changed covers
    modified/added/untracked/renamed-to (anything FsIngestSource.documents(only=...)
    should re-embed), deleted covers anything gone from the working tree
    (anything Retriever.delete() should purge). Used to reindex only what's
    about to be committed instead of walking the whole repo.

    A plain `git diff --name-status HEAD` misses brand-new files that were
    never `git add`ed -- they're not in HEAD *or* the index, so diffing
    against HEAD alone shows nothing for them; `git ls-files --others` covers
    exactly that gap.
    """
    diff = subprocess.run(
        ["git", "diff", "--name-status", "HEAD"],
        cwd=repo_path, capture_output=True, text=True, timeout=30,
    )
    if diff.returncode != 0:
        raise RuntimeError(f"git diff failed in {repo_path}: {diff.stderr.strip()}")
    changed: list[str] = []
    deleted: list[str] = []
    for line in diff.stdout.splitlines():
        if not line.strip():
            continue
        status, *paths = line.split("\t")
        if status == "D":
            deleted.append(paths[0])
        elif status.startswith("R"):  # "R100\told\tnew"
            deleted.append(paths[0])
            changed.append(paths[1])
        else:  # M, A, C, T, ...
            changed.append(paths[-1])

    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=repo_path, capture_output=True, text=True, timeout=30,
    )
    if untracked.returncode != 0:
        raise RuntimeError(f"git ls-files failed in {repo_path}: {untracked.stderr.strip()}")
    changed.extend(p for p in untracked.stdout.splitlines() if p.strip())

    return changed, deleted
