"""Layering guard: lower layers must not import upward into ``ciqkit.sim``.

An ``ast`` walk, so function-local imports (the lazy-import idiom used all
over this package) count exactly like top-level ones.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "ciqkit"
STDLIB = set(sys.stdlib_module_names) | {"__future__"}


def _imports(path: Path):
    """Yield absolute dotted module names imported anywhere in the file."""
    rel = path.relative_to(SRC.parent).with_suffix("")
    pkg = list(rel.parts[:-1])
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg[: len(pkg) - (node.level - 1)]
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            yield mod
            # `from . import sim` / `from .. import sim` names a submodule.
            for a in node.names:
                yield f"{mod}.{a.name}"


def _is_sim(mod: str) -> bool:
    return mod == "ciqkit.sim" or mod.startswith("ciqkit.sim.")


def _files(*parts):
    base = SRC.joinpath(*parts)
    return [base] if base.suffix == ".py" else sorted(base.rglob("*.py"))


# Existing violations in files outside the package that introduced this test.
ALLOW: dict[str, str] = {}


def _sim_importers(files):
    bad = []
    for f in files:
        key = f.relative_to(SRC).as_posix()
        if key in ALLOW:
            continue
        bad += [f"{key}: {m}" for m in _imports(f) if _is_sim(m)]
    return bad


def test_capture_and_qa_do_not_import_sim():
    assert _sim_importers(_files("capture") + _files("qa")) == []


def test_build_does_not_import_sim():
    assert _sim_importers(_files("build.py")) == []


def test_protocol_and_sdk_import_only_stdlib_and_allowed():
    allowed = {"ciqkit.paths", "ciqkit.sim.transport"}
    bad = []
    for f in _files("sim", "protocol.py") + _files("sdk.py"):
        for m in _imports(f):
            top = m.split(".")[0]
            if (top in STDLIB or m in ("ciqkit", "ciqkit.sim")
                    or any(m == a or m.startswith(a + ".") for a in allowed)):
                continue
            bad.append(f"{f.name}: {m}")
    assert bad == []


# Modules that are CLI entry points (dispatched from cli.py) and so print by design.
PRINT_OK_CLI = {
    "cli.py", "sim/client.py",  # `ciqkit sim` CLI
    "qa/layout.py", "qa/shot_score.py", "qa/icons.py",  # `ciqkit layout|shot-score|icon` mains
    "sim/simport.py",  # `ciqkit sim` port helper CLI
}
# Library modules that print today; allowlisted, not refactored.
PRINT_ALLOW: dict[str, str] = {
    "gifretime.py": "gifretime CLI main() prints the output paths",
    "leases.py": "stderr notice while waiting on a held lease",
    "sim/preflight.py": "stderr WARN lines from preflight",
    "sim/mcp_server.py": "stderr diagnostic; stdout is the JSON-RPC channel",
    "sim/storage.py": "StorageRun progress to stderr",
    "sim/broker.py": "daemon startup/shutdown lines to stderr (ciqsimd main)",
}


def _print_calls(path: Path):
    return [n.lineno for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "print"]


def test_library_modules_do_not_print():
    """stdout belongs to the CLI (JSON) and the MCP server (JSON-RPC)."""
    bad = []
    for f in sorted(SRC.rglob("*.py")):
        key = f.relative_to(SRC).as_posix()
        if (key.startswith("templates/") or key.endswith("_cli.py")
                or key in PRINT_OK_CLI or key in PRINT_ALLOW):
            continue
        bad += [f"{key}:{ln}" for ln in _print_calls(f)]
    assert bad == []
