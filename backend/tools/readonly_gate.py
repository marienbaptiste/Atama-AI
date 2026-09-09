"""Golden Rule gate (spec §0): NEVER set or write through the WaniKani or Bunpro API keys.

Static scan, no network. Runs as the first target of `make test`, before `make run` and
`make doctor`, and from the pre-commit hook. Exit 0 = OK, exit 1 = violations printed.

There is no bypass: no flag, no env var, no marker comment. Editing this file requires a new
ADR entry superseding ADR-021 — which that ADR says will not be written.

Usage:  python -m backend.tools.readonly_gate [repo_root]
"""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

HTTP_LIBS = {"httpx", "requests", "aiohttp", "urllib", "urllib3", "http"}
WRITE_METHODS = {"post", "put", "patch", "delete", "send", "stream"}  # `stream`/`send` take a method
SETTER_PREFIXES = (
    "set_", "write_", "update_", "create_", "delete_", "submit_", "start_",
    "post_", "put_", "patch_", "mark_", "reset_", "assign_",
)
FORBIDDEN_PARAMS = {"base_url", "base_origin", "api_url", "origin", "host", "endpoint_url"}
ALLOWED_URL_PREFIXES = ("https://api.wanikani.com", "https://api.bunpro.jp", "https://bunpro.jp")
# The exact read-tool surface of the Bunpro MCP server (spec §5). Nothing else may be registered.
MCP_READ_TOOLS = frozenset({"get_review_queue", "get_ghost_reviews", "get_grammar_progress"})
# WaniKani write scopes (spec §0 rule 6). May appear in code ONLY in the doctor's warning check.
_WRITE_SCOPE_PARTS = (("assignments", "start"), ("reviews", "create"), ("study_materials", "create"),
                      ("study_materials", "update"), ("user", "update"))
WRITE_SCOPES = tuple(":".join(p) for p in _WRITE_SCOPE_PARTS)
CODE_SUFFIXES = {".py", ".ts", ".js", ".mjs", ".json", ".yaml", ".yml", ".toml", ".sh"}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", ".cache", "logs", "dist", "__pycache__",
             ".pytest_cache", ".mypy_cache", ".ruff_cache"}


@dataclass(frozen=True)
class Violation:
    rule: int
    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path.as_posix()}:{self.line}: [rule {self.rule}] {self.message}"


def _iter_files(root: Path, suffixes: set[str]) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.is_file() and (p.suffix in suffixes or p.name == "Makefile"):
            out.append(p)
    return sorted(out)


def _imports_http_lib(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] in HTTP_LIBS for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in HTTP_LIBS:
                return True
    return False


def _imports_srs(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("backend.srs"):
            return True
        if isinstance(node, ast.Import) and any(a.name.startswith("backend.srs") for a in node.names):
            return True
    return False


def _literal_str(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _check_http_calls(path: Path, tree: ast.AST, out: list[Violation]) -> None:
    """Rule 1: no write-method calls; `.request(method)` must be the literal "GET"."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
        if name is None:
            continue
        if name in WRITE_METHODS:
            out.append(Violation(1, path, node.lineno, f"call to `.{name}(` — only GET is permitted"))
        elif name in ("request", "build_request"):
            method_node = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "method"), None)
            lit = _literal_str(method_node)
            if lit is None or lit.upper() != "GET":
                out.append(Violation(1, path, node.lineno, f"`.{name}(` with non-literal or non-GET method"))
        # method="..." keyword on anything else (e.g. client.stream(method=...))
        for kw in node.keywords:
            if kw.arg == "method":
                lit = _literal_str(kw.value)
                if lit is None or lit.upper() != "GET":
                    out.append(Violation(1, path, node.lineno, "`method=` keyword that is not the literal \"GET\""))


def _check_setter_names(path: Path, tree: ast.AST, out: list[Violation]) -> None:
    """Rule 2: no setter-shaped names in srs/**, regardless of behaviour."""
    for node in ast.walk(tree):
        names: list[tuple[str, int]] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append((node.name, node.lineno))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    names.append((t.id, node.lineno))
                elif isinstance(t, ast.Attribute):
                    names.append((t.attr, node.lineno))
        for name, line in names:
            if name.lower().startswith(SETTER_PREFIXES):
                out.append(Violation(2, path, line, f"setter-shaped name `{name}` — no setters in srs/ (rename or remove)"))


def _check_forbidden_params_and_urls(path: Path, tree: ast.AST, out: list[Violation]) -> None:
    """Rule 7: origins are constants; no base-url parameters, no foreign URL literals, no env reads."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in node.args.args + node.args.kwonlyargs:
                if arg.arg.lower() in FORBIDDEN_PARAMS:
                    out.append(Violation(7, path, node.lineno, f"parameter `{arg.arg}` — origins are constants, never parameters"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value
            if (v.startswith("http://") or v.startswith("https://")) and not v.startswith(ALLOWED_URL_PREFIXES):
                out.append(Violation(7, path, node.lineno, f"URL literal {v!r} is not a pinned origin"))
        elif isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv") and path.name == "http.py":
            out.append(Violation(7, path, node.lineno, "srs/http.py must not read the environment"))


def _check_client_surface(path: Path, tree: ast.AST, out: list[Violation]) -> None:
    """Rule 4: SrsClient exposes exactly one public method, `get`, and the guard transport exists."""
    found_client = False
    found_guard = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "SrsClient":
            found_client = True
            public = sorted(n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_"))
            if public != ["get"]:
                out.append(Violation(4, path, node.lineno, f"SrsClient public methods must be exactly ['get'], found {public}"))
        if isinstance(node, ast.ClassDef) and node.name == "_GuardTransport":
            found_guard = True
    if not found_client:
        out.append(Violation(4, path, 1, "srs/http.py must define SrsClient"))
    if not found_guard:
        out.append(Violation(4, path, 1, "srs/http.py must define _GuardTransport"))


def _check_mcp_tools(path: Path, tree: ast.AST, out: list[Violation]) -> None:
    """Rule 5: the MCP server registers only the three read tools."""
    registered: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            fname = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else "")
            if fname in ("tool", "add_tool", "Tool"):
                # @server.tool(name="x") / @server.tool("x") / Tool(name="x") / add_tool(fn, name="x")
                for kw in node.keywords:
                    if kw.arg == "name" and _literal_str(kw.value) is not None:
                        registered.add(_literal_str(kw.value))
                if node.args and _literal_str(node.args[0]):
                    registered.add(_literal_str(node.args[0]))
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "READ_TOOLS" for t in node.targets):
            if isinstance(node.value, (ast.Tuple, ast.List, ast.Set)):
                for elt in node.value.elts:
                    lit = _literal_str(elt)
                    if lit:
                        registered.add(lit)
    extra = registered - MCP_READ_TOOLS
    for name in sorted(extra):
        out.append(Violation(5, path, 1, f"MCP tool {name!r} is not one of the three read tools"))


def _check_scope_strings(path: Path, text: str, out: list[Violation]) -> None:
    """Rule 6: write-scope strings only in the doctor."""
    if path.name == "doctor.py" and path.parent.name == "tools":
        return
    for i, line in enumerate(text.splitlines(), 1):
        for scope in WRITE_SCOPES:
            if scope in line:
                out.append(Violation(6, path, i, f"WaniKani write scope string {scope!r} outside the doctor"))


def run(root: Path) -> list[Violation]:
    root = root.resolve()
    backend = root / "backend"
    srs_dir = backend / "srs"
    out: list[Violation] = []
    py_files = _iter_files(backend, {".py"}) if backend.exists() else []
    http_py = srs_dir / "http.py"

    for path in py_files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as e:
            out.append(Violation(0, path, e.lineno or 1, f"syntax error: {e.msg}"))
            continue
        in_srs = srs_dir in path.parents
        rel = path.relative_to(root)
        if in_srs or _imports_srs(tree):
            _check_http_calls(rel, tree, out)
        if in_srs:
            _check_setter_names(rel, tree, out)
            _check_forbidden_params_and_urls(rel, tree, out)
            if path != http_py and _imports_http_lib(tree):
                out.append(Violation(3, rel, 1, "HTTP library import outside srs/http.py"))
        if path == http_py:
            _check_client_surface(rel, tree, out)
        if in_srs and path.name == "bunpro_mcp.py":
            _check_mcp_tools(rel, tree, out)

    if srs_dir.exists() and not http_py.exists():
        out.append(Violation(4, srs_dir.relative_to(root), 1, "srs/ exists without srs/http.py"))

    for path in _iter_files(root, CODE_SUFFIXES):
        if path.name == "readonly_gate.py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        _check_scope_strings(path.relative_to(root), text, out)

    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[0]) if argv else Path(__file__).resolve().parents[2]
    violations = run(root)
    scanned = len(_iter_files(root, CODE_SUFFIXES))
    if violations:
        print(f"readonly-gate: {len(violations)} violation(s) of the Golden Rule (spec section 0):", file=sys.stderr)
        for v in violations:
            print("  " + str(v).encode("ascii", "replace").decode("ascii"), file=sys.stderr)
        return 1
    print(f"readonly-gate: 7 rules, {scanned} files, OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
