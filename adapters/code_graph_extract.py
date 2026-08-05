"""Adapter (Prompt 27): a SEPARATE tree-sitter analysis pass over Java/TypeScript
source, extracting call/implements/import edges for core.code_graph. Does not
modify adapters/ingest_fs.py -- reuses its container/member grammar spec and
name-extraction helpers (_GRAMMAR_BY_EXT, _ts_declaration, _ts_name) as
read-only imports, so a symbol's name here always matches the same symbol's
name in the main chunk index (needed for citations to line up between a Q&A
answer and a "what calls X" graph lookup).

Scoped to Java + TypeScript only, per explicit request -- JS/JSX use the same
grammar spec in adapters/ingest_fs.py but are deliberately excluded here.

Best-effort throughout: no type resolution, so a callee is usually only known
by its bare (unqualified) method name, and heritage/import extraction skips
silently on any tree shape it doesn't recognize rather than guessing. See
core/code_graph.py's module docstring for what this means for accuracy.
"""
from __future__ import annotations
from pathlib import Path
from typing import Iterable
from tree_sitter import Node, Parser
from adapters.ingest_fs import _GRAMMAR_BY_EXT, _ts_declaration, _ts_name
from core.code_graph import GraphEdge, GraphNode

_EXTENSIONS = (".java", ".ts", ".tsx")
_EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".data", ".idea", ".claude",
    ".chainlit", "node_modules", ".pytest_cache", "dist", "build", "target",
}
_CALL_TYPES = {"method_invocation", "call_expression"}
_JAVA_HERITAGE_FIELDS = ("superclass", "interfaces", "super_interfaces")
_TS_HERITAGE_TYPES = {"class_heritage", "extends_clause", "implements_clause"}

def _walk_repo(repo_path: Path) -> Iterable[Path]:
    for path in repo_path.rglob("*"):
        if not path.is_file() or path.suffix not in _EXTENSIONS:
            continue
        if _EXCLUDED_DIRS & set(path.relative_to(repo_path).parts[:-1]):
            continue
        yield path

def _callee_name(call_node: Node, lang_tag: str) -> str | None:
    """Best-effort callee name -- no receiver-type resolution, just the
    syntactic target. None for shapes not recognized (e.g. an immediately-
    invoked function expression), which the caller skips rather than guesses."""
    if lang_tag == "java":
        name = call_node.child_by_field_name("name")
        return name.text.decode("utf-8") if name is not None else None
    fn = call_node.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "identifier":
        return fn.text.decode("utf-8")
    if fn.type == "member_expression":
        prop = fn.child_by_field_name("property")
        return prop.text.decode("utf-8") if prop is not None else None
    return None

def _find_calls(body: Node, lang_tag: str) -> list[tuple[str, int]]:
    """Every call site inside `body` as (callee_name, 1-indexed line) --
    recurses the whole subtree, since a call is typically nested several
    levels deep in an expression/statement, not a direct child."""
    calls: list[tuple[str, int]] = []
    stack = [body]
    while stack:
        node = stack.pop()
        if node.type in _CALL_TYPES:
            name = _callee_name(node, lang_tag)
            if name:
                calls.append((name, node.start_point[0] + 1))
        stack.extend(node.children)
    return calls

def _heritage_names(decl: Node, lang_tag: str) -> list[str]:
    """Best-effort supertype/interface names for an 'implements' edge. Only
    descends into fields/children recognized as heritage clauses (never the
    class body) -- an unrecognized shape just means a missing edge, not a
    wrong one."""
    names: list[str] = []
    body = decl.child_by_field_name("body")
    for child in decl.children:
        if child is body:
            continue
        if lang_tag == "java" and child.type not in _JAVA_HERITAGE_FIELDS:
            continue
        if lang_tag != "java" and child.type not in _TS_HERITAGE_TYPES:
            continue
        stack = [child]
        while stack:
            n = stack.pop()
            if n.type in ("type_identifier", "identifier"):
                names.append(n.text.decode("utf-8"))
            stack.extend(n.children)
    return list(dict.fromkeys(names))

def _imports(tree_root: Node, lang_tag: str) -> list[str]:
    imports: list[str] = []
    for node in tree_root.children:
        if lang_tag == "java" and node.type == "import_declaration":
            text = node.text.decode("utf-8").strip().removeprefix("import").strip().rstrip(";").strip()
            text = text.removeprefix("static ").strip()
            if text:
                imports.append(text)
        elif lang_tag != "java" and node.type == "import_statement":
            src = node.child_by_field_name("source")
            if src is not None:
                imports.append(src.text.decode("utf-8").strip("'\""))
    return imports

def extract_file(path: Path, rel_path: str) -> tuple[list[GraphNode], list[GraphEdge]]:
    spec = _GRAMMAR_BY_EXT.get(path.suffix)
    if spec is None:
        return [], []
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [], []
    if not source.strip():
        return [], []

    parser = Parser(spec.language)
    tree = parser.parse(source.encode("utf-8"))
    if tree.root_node.has_error:
        return [], []

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []

    for imp in _imports(tree.root_node, spec.lang_tag):
        edges.append(GraphEdge(src=rel_path, dst=imp, kind="imports", src_path=rel_path, src_line=1))

    for top in tree.root_node.children:
        decl = _ts_declaration(top)

        if decl.type in spec.top_level or decl.type in ("lexical_declaration", "variable_declaration"):
            # Top-level function, or Angular's functional-guard/interceptor
            # pattern (`export const authGuard = (route, state) => {...}`) --
            # mirrors adapters/ingest_fs.py's _ts_chunks handling of the same
            # shapes, so a call made from one of these (very common in
            # modern Angular/TS) isn't silently invisible to the graph.
            fn_node, fn_name = None, None
            if decl.type in spec.top_level:
                fn_node, fn_name = decl, _ts_name(decl)
            else:
                declarators = [c for c in decl.children if c.type == "variable_declarator"]
                if len(declarators) == 1:
                    value = declarators[0].child_by_field_name("value")
                    if value is not None and value.type in ("arrow_function", "function_expression"):
                        fn_node, fn_name = value, _ts_name(declarators[0])
            if fn_name is not None and fn_node is not None:
                start, end = decl.start_point[0] + 1, decl.end_point[0] + 1
                nodes.append(GraphNode(symbol=fn_name, path=rel_path, start_line=start, end_line=end, language=spec.lang_tag))
                for callee, line in _find_calls(fn_node, spec.lang_tag):
                    edges.append(GraphEdge(src=fn_name, dst=callee, kind="calls", src_path=rel_path, src_line=line))
            continue

        if decl.type not in spec.containers:
            continue
        class_name = _ts_name(decl) or "<anonymous>"
        start, end = decl.start_point[0] + 1, decl.end_point[0] + 1
        nodes.append(GraphNode(symbol=class_name, path=rel_path, start_line=start, end_line=end, language=spec.lang_tag))

        for iface in _heritage_names(decl, spec.lang_tag):
            edges.append(GraphEdge(src=class_name, dst=iface, kind="implements", src_path=rel_path, src_line=start))

        body = decl.child_by_field_name("body")
        if body is None:
            continue
        for member in body.children:
            if member.type not in spec.members:
                continue
            member_name = _ts_name(member)
            if member_name is None:
                continue
            symbol = f"{class_name}.{member_name}"
            m_start, m_end = member.start_point[0] + 1, member.end_point[0] + 1
            nodes.append(GraphNode(symbol=symbol, path=rel_path, start_line=m_start, end_line=m_end, language=spec.lang_tag))
            for callee, line in _find_calls(member, spec.lang_tag):
                edges.append(GraphEdge(src=symbol, dst=callee, kind="calls", src_path=rel_path, src_line=line))

    return nodes, edges

def extract_repo(repo_path: str) -> tuple[list[GraphNode], list[GraphEdge]]:
    """Walks repo_path for .java/.ts/.tsx files and extracts nodes+edges from
    each. Best-effort per file -- a file that fails to parse (has_error) or
    can't be decoded is silently skipped, same tolerance adapters/ingest_fs.py
    already has for malformed input."""
    root = Path(repo_path).resolve()
    all_nodes: list[GraphNode] = []
    all_edges: list[GraphEdge] = []
    for path in _walk_repo(root):
        rel_path = str(path.relative_to(root)).replace("\\", "/")
        nodes, edges = extract_file(path, rel_path)
        all_nodes.extend(nodes)
        all_edges.extend(edges)
    return all_nodes, all_edges
