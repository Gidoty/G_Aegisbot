"""Result-affecting source code, found from the code itself instead of from a hand-kept list.

The identity of the code behind a backtest is the SHA-256 of every Python file in the STATIC IMPORT CLOSURE of the backtest's
entry points, found by parsing the files (`ast`), not by importing them:

  backtest/__main__.py            the command line (`python -m g_aegisbot.backtest`)
  backtest/simulator.py           the library entry (`Backtester`)
  backtest/acceptance_methods.py  the protocol's calculations, which turn a run into acceptance numbers

For every module reached: the file itself, and the `__init__.py` of every package above it (Python runs those on import).
Every kind of import is followed, including ones inside functions, under `if`/`try`, and `TYPE_CHECKING` blocks, because
over-hashing is safe and under-hashing is not. A module reached only to be imported is still hashed (for example the live
adapter, which `engine/strategy.py` imports at load time): nothing is ruled out by judgement.

Files NOT reachable from an entry point are not part of the identity (the web API, database layer, live runner and so on).
That is a property of the code, checked on every run, not a list somebody maintains.

Fails closed (SourceClosureError), never guessing, when it meets: an internal import that resolves to no file, a relative
import that leaves the package, a dynamic import it cannot read (`__import__`, `importlib.import_module` with a non-literal
name, `spec_from_file_location`, `load_module`), a file that cannot be parsed or decoded, a protected path, or a third-party
import whose top-level name is not in DECLARED_EXTERNAL. Third-party libraries are not hashed; their installed versions are
recorded and enter the experiment identity instead.
"""
from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path, PurePosixPath

from g_aegisbot.backtest.protected import assert_not_protected

ENTRY_POINTS = ("backtest/__main__.py", "backtest/simulator.py", "backtest/acceptance_methods.py")
# Third-party top-level imports allowed in the closure. Anything else is an unclassified dependency and fails closed.
DECLARED_EXTERNAL = frozenset({"ccxt", "cryptography", "dotenv", "numpy", "pandas"})
_DYNAMIC_CALLS = {"__import__", "import_module", "spec_from_file_location", "load_module"}


class SourceClosureError(Exception):
    """The static import closure cannot be established with certainty."""


def _module_file(root: Path, parts: list[str]) -> str | None:
    rel = "/".join(parts)
    for cand in ([f"{rel}.py", f"{rel}/__init__.py"] if rel else ["__init__.py"]):
        if (root / cand).is_file():
            return cand
    return None


def _imports_of(rel: str, tree: ast.AST, pkg: str) -> list[tuple[str, list[str]]]:
    """(absolute module name, imported names) for every import in the file; raises on dynamic imports."""
    out: list[tuple[str, list[str]]] = []
    here = list(PurePosixPath(rel).parent.parts)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(a.name, []) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level - 1 > len(here):
                    raise SourceClosureError(f"{rel}:{node.lineno} a relative import leaves the package")
                base = here[:len(here) - (node.level - 1)]
                name = ".".join([pkg, *base, *([node.module] if node.module else [])])
            else:
                name = node.module or ""
            out.append((name, [a.name for a in node.names]))
        elif isinstance(node, ast.Call):
            f = node.func
            called = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if called in _DYNAMIC_CALLS:
                arg = node.args[0] if node.args else None
                if called in ("__import__", "import_module") and isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    out.append((arg.value, []))
                else:
                    raise SourceClosureError(f"{rel}:{node.lineno} dynamic import ({called}) cannot be analysed")
    return out


def closure(package_dir: str | Path, entry_points=ENTRY_POINTS, *, package_name: str | None = None,
            declared_external=DECLARED_EXTERNAL) -> dict:
    """{'files': {relative path: sha256}, 'external': sorted third-party top-level names, 'entry_points': [...]}."""
    root = Path(package_dir)
    pkg = package_name or root.name
    stdlib = set(sys.stdlib_module_names)
    files: dict[str, str] = {}
    external: set[str] = set()
    todo = list(entry_points)
    while todo:
        rel = todo.pop()
        if rel in files:
            continue
        path = root / rel
        assert_not_protected(path)
        if not path.is_file():
            raise SourceClosureError(f"{rel} is not a file")
        try:
            data = path.read_bytes()
            tree = ast.parse(data)
        except (OSError, SyntaxError, ValueError) as exc:
            raise SourceClosureError(f"{rel} cannot be read or parsed ({type(exc).__name__})") from exc
        files[rel] = hashlib.sha256(data).hexdigest()
        for parent in list(PurePosixPath(rel).parents)[:-1] + [PurePosixPath(".")]:
            init = _module_file(root, list(parent.parts))
            if init and init != rel and (parent.parts or init == "__init__.py"):
                todo.append(init)
        for name, names in _imports_of(rel, tree, pkg):
            top = name.split(".")[0]
            if top == pkg:
                parts = name.split(".")[1:]
                target = _module_file(root, parts)
                if target is None:
                    raise SourceClosureError(f"{rel} imports {name}, which resolves to no file in the package")
                todo.append(target)
                for sub in names:                                   # `from pkg import module` pulls in the module too
                    s = _module_file(root, parts + [sub])
                    if s and s != target:
                        todo.append(s)
            elif top in stdlib or name == "":
                continue
            elif top in declared_external:
                external.add(top)
            else:
                raise SourceClosureError(f"{rel} imports the unclassified third-party module {top!r}")
    return {"files": dict(sorted(files.items())), "external": sorted(external), "entry_points": list(entry_points)}
