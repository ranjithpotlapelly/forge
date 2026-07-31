"""Smoke test: proves adapters/ingest_fs.py's tree-sitter chunking
(_ts_chunks/_GRAMMAR_BY_EXT) gives JavaScript/TypeScript/Java the same
per-method chunk granularity Python already had via _python_chunks, instead
of one whole-file chunk per file -- see docs/PROMPTS.md's Prompt 1 cross-check
and the "Reinstate tree-sitter" plan for why this matters (product/code_edit.py
can only scope an edit as precisely as the index lets it). Go is intentionally
NOT covered -- not an active target repo, stays on whole-file chunking.

Checks, one inline fixture per case, no retriever/embeddings needed (this
only exercises FsIngestSource.documents() directly): (1) JS top-level
function + class methods, (2) TS class + interface (method_signature), (3)
TSX -- proves language_tsx() is actually wired, not plain typescript, by
using a JSX-in-method-body fixture that only parses cleanly under the tsx
sub-grammar, (4) Java -- the exact return-type-vs-method-name pitfall
(child_by_field_name("name") must yield "loadUserByUsername", never
"UserDetails") plus a constructor, (5) a class with only an
arrow-function-valued property (no method_definition node) falls back to one
whole-class chunk rather than indexing nothing, (6) unparseable source falls
back to one whole-file chunk via has_error, (7) `export class Foo {}` /
`export function f() {}` -- real Angular/TS code is almost entirely ES
module exports, which parse as an export_statement wrapping the actual
declaration; caught via a real-repo spot check (0 method chunks came out of
rag-frontend-angular-v2 on the first pass) and fixed by _ts_declaration()
unwrapping, (8) `export const authGuard: T = (route, state) => {...}` -- a
common modern Angular pattern (functional guards/interceptors) that's a
lexical_declaration bound to an arrow_function, not a function_declaration;
also caught via the same real-repo spot check, (9) .py dispatch is unchanged
(regression guard on _python_chunks).
Run from the repo root:  python -m app.ingest_ts_chunking_test
"""
from __future__ import annotations
import shutil
import sys
from pathlib import Path
from adapters.ingest_fs import FsIngestSource

SCRATCH = Path("./.data/ingest_ts_chunking_test").resolve()

def _write(rel: str, content: str) -> None:
    path = SCRATCH / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

def _docs(rel: str, languages: list[str]) -> list:
    ingest = FsIngestSource(str(SCRATCH), languages=languages)
    return list(ingest.documents(only=[rel]))

def main() -> int:
    shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir(parents=True)

    # --- 1. JavaScript: top-level function + class methods -----------------
    _write("sample.js", (
        "function standalone() {\n    return 1;\n}\n\n"
        "class Widget {\n    constructor() {\n        this.x = 1;\n    }\n"
        "    render() {\n        return this.x;\n    }\n}\n"
    ))
    docs = _docs("sample.js", ["javascript"])
    symbols = {d.metadata["symbol"] for d in docs}
    if symbols != {"standalone", "Widget.constructor", "Widget.render"}:
        print(f"[!!] JS: expected {{'standalone', 'Widget.constructor', 'Widget.render'}}, got {symbols}")
        return 1
    if any(d.metadata["language"] != "javascript" for d in docs):
        print(f"[!!] JS: not every chunk has language=javascript: {[d.metadata for d in docs]}")
        return 1
    print(f"[ok] JS: {sorted(symbols)}")

    # --- 2. TypeScript: class + interface method_signature -----------------
    _write("sample.ts", (
        "class Widget {\n    render(): void {\n        return;\n    }\n}\n\n"
        "interface Foo {\n    bar(): void;\n}\n"
    ))
    docs = _docs("sample.ts", ["typescript"])
    symbols = {d.metadata["symbol"] for d in docs}
    if "Foo.bar" not in symbols or "Widget.render" not in symbols:
        print(f"[!!] TS: expected 'Foo.bar' and 'Widget.render' in {symbols}")
        return 1
    if any(d.metadata["language"] != "typescript" for d in docs):
        print(f"[!!] TS: not every chunk has language=typescript: {[d.metadata for d in docs]}")
        return 1
    print(f"[ok] TS: {sorted(symbols)}")

    # --- 3. TSX: JSX in a method body -- proves language_tsx() is wired ----
    _write("sample.tsx", (
        "class Widget {\n    render() {\n        return <div>{this.x}</div>;\n    }\n}\n"
    ))
    docs = _docs("sample.tsx", ["typescript"])
    symbols = {d.metadata["symbol"] for d in docs}
    if symbols != {"Widget.render"}:
        print(f"[!!] TSX: expected {{'Widget.render'}} (JSX should parse cleanly under the tsx "
              f"sub-grammar), got {symbols} -- if this fell back to '<file>', language_tsx() "
              f"isn't actually being used for .tsx files")
        return 1
    print(f"[ok] TSX: {sorted(symbols)} (JSX parsed under the tsx sub-grammar, not plain typescript)")

    # --- 4. Java: the return-type-vs-method-name pitfall, plus a ctor ------
    _write("Foo.java", (
        "public class Foo {\n"
        "    public Foo() {\n    }\n\n"
        "    public UserDetails loadUserByUsername(String username) {\n"
        "        return null;\n    }\n}\n"
    ))
    docs = _docs("Foo.java", ["java"])
    symbols = {d.metadata["symbol"] for d in docs}
    if "Foo.UserDetails" in symbols:
        print(f"[!!] Java: symbol extraction picked the return type instead of the method name: {symbols}")
        return 1
    if symbols != {"Foo.Foo", "Foo.loadUserByUsername"}:
        print(f"[!!] Java: expected {{'Foo.Foo', 'Foo.loadUserByUsername'}}, got {symbols}")
        return 1
    print(f"[ok] Java: {sorted(symbols)} (return-type-vs-name pitfall avoided)")

    # --- 5. Zero-members fallback: arrow-function-valued property only -----
    _write("Bar.ts", (
        "class Bar {\n    load = (id: string) => {\n        return id;\n    };\n}\n"
    ))
    docs = _docs("Bar.ts", ["typescript"])
    if len(docs) != 1 or docs[0].metadata["symbol"] != "Bar":
        print(f"[!!] zero-members fallback: expected exactly one chunk symbol='Bar', "
              f"got {[d.metadata for d in docs]}")
        return 1
    print("[ok] zero-members fallback: whole-class chunk for a class with no method_definition node")

    # --- 6. Whole-file fallback on unparseable input ------------------------
    _write("broken.ts", "class Foo { method( {\n")
    docs = _docs("broken.ts", ["typescript"])
    if len(docs) != 1 or docs[0].metadata["symbol"] != "<file>" or docs[0].metadata["language"] != "typescript":
        print(f"[!!] unparseable fallback: expected one '<file>' chunk with language=typescript, "
              f"got {[d.metadata for d in docs]}")
        return 1
    print("[ok] unparseable input falls back to one whole-file chunk via has_error")

    # --- 7. export-wrapped class/function (the real-repo bug) --------------
    _write("Exported.ts", (
        "export class Exported {\n    render(): void {\n        return;\n    }\n}\n\n"
        "export function helper(): void {\n    return;\n}\n"
    ))
    docs = _docs("Exported.ts", ["typescript"])
    symbols = {d.metadata["symbol"] for d in docs}
    if symbols != {"Exported.render", "helper"}:
        print(f"[!!] export unwrap: expected {{'Exported.render', 'helper'}}, got {symbols} "
              f"-- export_statement isn't being unwrapped before classification")
        return 1
    print(f"[ok] export-wrapped class/function: {sorted(symbols)}")

    # --- 8. export const arrow function (Angular functional guard shape) ---
    _write("guard.ts", (
        "export const authGuard: CanActivateFn = (route, state) => {\n"
        "    return true;\n};\n\n"
        "export const PLAIN_CONST = 42;\n"
    ))
    docs = _docs("guard.ts", ["typescript"])
    symbols = {d.metadata["symbol"] for d in docs}
    if "authGuard" not in symbols:
        print(f"[!!] export const arrow function: expected 'authGuard' in {symbols}")
        return 1
    if "PLAIN_CONST" in symbols:
        print(f"[!!] export const arrow function: a plain data constant should NOT be "
              f"treated as a chunkable leaf, got {symbols}")
        return 1
    print(f"[ok] export const arrow function: {sorted(symbols)} (plain data const correctly excluded)")

    # --- 9. Python dispatch unchanged (regression guard) --------------------
    _write("sample.py", "def top():\n    return 1\n\n\nclass C:\n    def m(self):\n        return 2\n")
    docs = _docs("sample.py", ["python"])
    symbols = {d.metadata["symbol"] for d in docs}
    if symbols != {"top", "C"}:
        print(f"[!!] Python regression: .py dispatch changed, expected {{'top', 'C'}}, got {symbols}")
        return 1
    print(f"[ok] Python: {sorted(symbols)} (dispatch unchanged, still via _python_chunks)")

    shutil.rmtree(SCRATCH, ignore_errors=True)
    print("\nTree-sitter chunking OK: JS/TS/TSX/Java get per-method chunks, Python untouched.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
