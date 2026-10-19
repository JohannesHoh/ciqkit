"""``ciqkit doctor``: environment and (optionally) project checks.

The report is data first -- ``run()`` returns a dict the CLI renders and the
MCP ``ciqsim_doctor`` tool serialises -- so both surfaces say the same thing.
No stdout here: the MCP server speaks JSON-RPC on it. Never ``sys.exit``.

Report shape::

    {"ok": bool, "version": str,
     "checks": [{"name", "status": "ok"|"warn"|"fail"|"info",
                 "detail": str, "notes": [str, ...]}]}

Only ``fail`` makes ``ok`` false; ``warn`` and ``info`` are advice.
"""
from __future__ import annotations

import os
import re
import sys
import xml.etree.ElementTree as ET

from . import __version__

_STATUSES = ("ok", "warn", "fail", "info")


def _check(name: str, status: str, detail: str = "", notes=None) -> dict:
    assert status in _STATUSES
    return {"name": name, "status": status, "detail": detail,
            "notes": list(notes or [])}


def check_console_scripts() -> tuple[bool, list[str]]:
    """Are this install's declared console scripts actually usable?

    The three ways one goes wrong are all invisible to the test suite, and all
    fatal at the moment a user follows the README:

      declared but not installed  — a long-lived editable install predates the
          entry point, so `pip install -e .` was never re-run. Happened to
          a console script on the day it was added, in two venvs at once.
      installed but broken        — the target module was renamed or removed and
          the script still points at it.
      shadowed                    — an older copy of the same script earlier on
          PATH wins. A live risk here: more than one venv can hold ciqkit
          installed editable from this same source.

    Resolving through importlib.metadata catches the first two; comparing with
    shutil.which catches the third. A test cannot do any of it — a fresh CI venv
    always has exactly the right scripts, which is why this lives in doctor.

    Returns (ok, lines_to_print).
    """
    import importlib.metadata as md
    import pathlib
    import shutil

    out: list[str] = []
    ok = True
    try:
        eps = [e for e in md.distribution("ciqkit").entry_points
               if e.group == "console_scripts"]
    except md.PackageNotFoundError:
        return True, ["  ciqkit is not installed as a distribution (running from "
                      "source?) — console scripts cannot be checked"]
    except Exception as exc:      # never let a diagnostic break the diagnostic
        return True, [f"  could not be checked: {exc}"]

    if not eps:
        return False, ["  FAIL: this install declares no console scripts at all"]

    for ep in sorted(eps, key=lambda e: e.name):
        try:
            ep.load()
        except Exception as exc:
            ok = False
            out.append(f"  {ep.name:<18} BROKEN — {ep.value} does not load: {exc}")
            continue
        # Authoritative test: did the install actually write the script next to
        # this interpreter? PATH is NOT that test — a venv that was never
        # activated has none of its scripts on PATH, and calling that a failure
        # would cry wolf on a perfectly good install.
        expected = pathlib.Path(sys.executable).parent / (
            ep.name + (".exe" if sys.platform == "win32" else ""))
        if not expected.exists():
            ok = False
            out.append(f"  {ep.name:<18} NOT INSTALLED — declared as {ep.value}, "
                       f"but {expected} does not exist. Re-run "
                       f"`pip install -e .` in this venv.")
            continue
        found = shutil.which(ep.name)
        if found and pathlib.Path(found).resolve() != expected.resolve():
            ok = False
            out.append(f"  {ep.name:<18} SHADOWED — PATH finds {found}, this "
                       f"interpreter's copy is {expected}")
            continue
        where = "on PATH" if found else "not on PATH (venv not activated)"
        out.append(f"  {ep.name:<18} ok  ({expected}, {where})")
    return ok, out


def _environment_checks(checks: list) -> None:
    import platform
    from .sim import preflight
    from . import paths

    checks.append(_check(
        "ciqkit", "info",
        f"{__version__}  ({platform.system()} {platform.release()}, "
        f"python {platform.python_version()})"))

    try:
        checks.append(_check("runtime dir", "ok",
                             f"{paths.runtime_dir()}  ({paths.privacy_note()})"))
    except Exception as exc:
        checks.append(_check("runtime dir", "fail", f"FAIL: {exc}"))

    sdk = preflight.detect_sdk()
    checks.append(_check("Connect IQ SDK", "ok" if sdk else "info",
                         str(sdk or "NOT FOUND")))

    # A seed patches files in your project. If one is still recorded as
    # outstanding, say so here rather than letting a stray SEED_* flag reach a
    # commit -- this is the command people run when something looks off.
    try:
        from .sim import seedjournal
        if seedjournal.owner_alive():
            checks.append(_check("seed", "info",
                                 "a job is holding a seed right now "
                                 "(it restores when the job ends)"))
        else:
            stale = seedjournal.stale()
            if stale:
                notes = []
                for e in stale:
                    notes.append(f"  {e['path']}\n    original bytes kept at: "
                                 f"{e['backup']}")
                checks.append(_check(
                    "seed", "fail",
                    "NOT RESTORED — ciqkit patched these files and they changed "
                    "before it could put them back:", notes))
            elif seedjournal.pending():
                checks.append(_check(
                    "seed", "warn",
                    "a patch from a dead process is pending; "
                    "run `ciqkit sim restore`"))
    except Exception as exc:   # never let a diagnostic break the diagnostic
        checks.append(_check("seed", "warn", f"could not be checked: {exc}"))

    preflight.reset()
    try:
        res = preflight.check_once()
        checks.append(_check("simulator", "ok", "preflight OK",
                             [f"  warning: {w}" for w in res.get("warnings", [])]))
    except preflight.PreflightError as exc:
        checks.append(_check("simulator", "fail", f"FAIL\n  {exc}"))

    scripts_ok, script_lines = check_console_scripts()
    checks.append(_check("console scripts", "ok" if scripts_ok else "fail", "",
                         script_lines))

    for name, mod, why in (
        ("Pillow", "PIL", "required for any screenshot work"),
        ("numpy", "numpy", "required for layout and quality checks"),
        ("opencv (cv2)", "cv2", "optional: sharper watch-face detection"),
        ("ocrmac", "ocrmac", "optional: on-screen text verification (macOS)"),
        ("winocr", "winocr", "optional: on-screen text verification (Windows)"),
        ("pytesseract", "pytesseract", "optional: OCR fallback"),
    ):
        if mod == "ocrmac" and sys.platform == "win32":
            continue
        if mod == "winocr" and sys.platform != "win32":
            continue
        try:
            __import__(mod)
            checks.append(_check(name, "ok", "installed"))
        except ImportError:
            checks.append(_check(name, "info", f"missing    ({why})"))


# ── project checks ─────────────────────────────────────────────────────────

_TEST_ANNOTATION = re.compile(r"\(\s*:test\s*\)")
# Build output and VCS dirs never hold the project's own sources.
_SKIP_DIRS = {"bin", "build", "gen", ".git", "node_modules", ".venv"}


def _find_tests(app_dir: str) -> list[str]:
    """Project-relative paths of .mc files that carry a ``(:test)`` annotation."""
    found = []
    for root, dirs, files in os.walk(app_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if not f.endswith(".mc"):
                continue
            path = os.path.join(root, f)
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    if _TEST_ANNOTATION.search(fh.read()):
                        found.append(os.path.relpath(path, app_dir))
            except OSError:
                continue
    return sorted(found)


def _manifest_check(app_dir: str) -> tuple[dict, list[str]]:
    from .build import MANIFEST_NS

    path = os.path.join(app_dir, "manifest.xml")
    if not os.path.isfile(path):
        return _check("manifest", "fail", f"FAIL: no manifest.xml in {app_dir}"), []
    try:
        root = ET.parse(path).getroot()  # noqa: S314 -- the user's own project file
    except (OSError, ET.ParseError) as exc:
        return _check("manifest", "fail", f"FAIL: manifest.xml does not parse: {exc}"), []
    app = next(root.iter(f"{{{MANIFEST_NS}}}application"), None)
    app_id = app.get("id") if app is not None else None
    products = [p.get("id") for p in root.iter(f"{{{MANIFEST_NS}}}product")
                if p.get("id")]
    problems = []
    if not app_id:
        problems.append("no <iq:application id=...> (app UUID)")
    if not products:
        problems.append("no <iq:product> entries")
    if problems:
        return _check("manifest", "fail", "FAIL: " + "; ".join(problems)), products
    return _check("manifest", "ok",
                  f"app id {app_id}, {len(products)} product"
                  f"{'' if len(products) == 1 else 's'}"), products


def project_checks(project: str) -> list[dict]:
    from . import build

    app_dir = os.path.abspath(os.path.expanduser(project))
    checks = [_check("project", "info", app_dir)]
    if not os.path.isdir(app_dir):
        checks.append(_check("manifest", "fail", "FAIL: not a directory"))
        return checks

    mcheck, products = _manifest_check(app_dir)
    checks.append(mcheck)

    # Which manifest products does the SDK actually have device files for?
    ddir = build.sdk_devices_dir()
    if not products:
        pass   # already reported by the manifest check
    elif not os.path.isdir(ddir):
        checks.append(_check("devices", "warn",
                             f"cannot verify: no SDK devices dir at {ddir or '(unknown on this platform)'}"))
    else:
        have = [p for p in products if os.path.isdir(os.path.join(ddir, p))]
        missing = [p for p in products if p not in have]
        if not have:
            checks.append(_check(
                "devices", "fail",
                "FAIL: none of the manifest's products are installed in the SDK",
                [f"  missing: {p}" for p in missing]))
        elif missing:
            checks.append(_check(
                "devices", "warn", f"{len(have)} of {len(products)} installed",
                [f"  warning: {p} is not installed in the SDK" for p in missing]))
        else:
            checks.append(_check("devices", "ok",
                                 f"all {len(products)} manifest products installed"))

    try:
        checks.append(_check("dev key", "ok", build.resolve_dev_key()))
    except build.BuildError as exc:
        checks.append(_check("dev key", "fail", f"FAIL: {exc}"))

    from . import config

    cfg = None
    try:
        cfg = config.load_config(app_dir)
    except config.ConfigError as exc:
        checks.append(_check("ciqkit.toml", "fail", f"FAIL: {exc}"))
    if cfg is not None and cfg.path:
        notes = []
        if cfg.device and cfg.device not in products and products:
            notes.append(f"  warning: [sim] device {cfg.device!r} is not in the "
                         f"manifest's products")
        n = len(cfg.screens)
        checks.append(_check(
            "ciqkit.toml", "warn" if notes else "ok",
            f"{cfg.path}  ({n} screen{'' if n == 1 else 's'})", notes))
    elif cfg is not None:
        checks.append(_check("ciqkit.toml", "info", "none (optional)"))

    if cfg is None:
        return checks   # build route and device both read the broken file
    route = build.build_route(app_dir)
    device = build.resolve_device(app_dir)
    try:
        argv, template = build.resolve_build(app_dir, device)
        if argv is not None:
            checks.append(_check("build", "ok",
                                 f"{route}  (monkeyc: {argv[0]})"))
        else:
            checks.append(_check("build", "ok", f"{route}  ({template})"))
    except build.BuildError as exc:
        checks.append(_check("build", "fail", f"{route}  FAIL: {exc}"))

    jungle = os.path.join(app_dir, "monkey.jungle")
    jungle_text = ""
    if os.path.isfile(jungle):
        try:
            with open(jungle, encoding="utf-8", errors="replace") as fh:
                jungle_text = fh.read()
        except OSError:
            pass
        checks.append(_check("monkey.jungle", "ok", jungle))
    else:
        # Fatal only for the built-in build, which is what needs it.
        status = "fail" if route == "built-in monkeyc" else "warn"
        checks.append(_check("monkey.jungle", status, f"{status.upper()}: missing"))

    tests = _find_tests(app_dir)
    jungle_mentions = "test" in jungle_text.lower()
    if not tests:
        checks.append(_check(
            "tests", "warn",
            "no (:test) functions found; `ciqkit sim test` would report 0 tests"))
    elif route != "built-in monkeyc" and not cfg.build_test_cmd:
        # Found at run time otherwise: session.build refuses to guess a test
        # variant of a custom command (it would "pass" with zero tests).
        checks.append(_check(
            "tests", "warn",
            f"(:test) in {len(tests)} file{'' if len(tests) == 1 else 's'}, but the "
            f"build is a custom command with no test variant: `ciqkit sim test` "
            f"needs `[build] test_cmd` in ciqkit.toml or --prg with a `monkeyc -t` "
            f"build"))
    elif jungle_mentions or any(t.split(os.sep)[0] == "source" for t in tests):
        checks.append(_check("tests", "ok", f"(:test) in {len(tests)} file"
                             f"{'' if len(tests) == 1 else 's'}"))
    else:
        checks.append(_check(
            "tests", "warn",
            f"(:test) found in {', '.join(tests[:3])} but monkey.jungle does not "
            f"mention a test source path, so a -t build may not include them"))
    return checks


def run(project: str | None = None) -> dict:
    """Run every check and return the report dict."""
    checks: list[dict] = []
    _environment_checks(checks)
    if project:
        try:
            checks.extend(project_checks(project))
        except Exception as exc:   # never let a diagnostic break the diagnostic
            checks.append(_check("project", "fail", f"could not be checked: {exc}"))
    return {"ok": all(c["status"] != "fail" for c in checks),
            "version": __version__, "checks": checks}


def render(report: dict) -> str:
    lines = []
    for c in report["checks"]:
        if c["name"] == "ciqkit":
            lines.append(f"ciqkit {c['detail']}")
        else:
            lines.append(f"{c['name']:<16} {c['detail']}".rstrip())
        lines.extend(c["notes"])
    return "\n".join(lines) + "\n"
