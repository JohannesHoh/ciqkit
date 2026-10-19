"""``ciqkit`` -- the single command-line entry point.

Subcommands are thin delegates to the module that owns each area, so
``ciqkit sim shot ...`` and ``python -m ciqkit.sim.client shot ...`` behave
identically.
"""
from __future__ import annotations

import sys

from . import __version__

_USAGE = """usage: ciqkit <command> [options]

commands:
  sim          drive the Connect IQ Simulator (screenshots, recordings, tests)
  layout       check captured watch screens for clipped or overflowing content
  shot-score   score screenshot image quality 0-100
  icon         launcher-icon checks and BETA-ribbon compositing
  storage      journal / reset / replay the simulator's per-app storage
  lease        run a command while holding a machine-wide resource lease
  new          create a new Connect IQ project from a template
  init         wire an existing project up for ciqkit (dry run unless --write)
  keygen       create the developer key used by the built-in build
  doctor       check that the environment can actually drive the simulator

Run `ciqkit <command> --help` for the options of each command.
"""


# subcommand -> (module holding its main(), the extra that makes it work).
# The base install deliberately has no dependencies, so half of these cannot
# import until the user picks an extra; `extra` is what we name when they do not.
_COMMANDS = {
    "sim": ("ciqkit.sim.client", "sim"),
    "layout": ("ciqkit.qa.layout", "checks"),
    "shot-score": ("ciqkit.qa.shot_score", "checks"),
    "icon": ("ciqkit.qa.icons", "checks"),
    "storage": ("ciqkit.sim.storage", None),
    "lease": ("ciqkit.lease_cli", None),
    "keygen": ("ciqkit.keygen_cli", None),
    "new": ("ciqkit.new_cli", None),
    "init": ("ciqkit.init_cli", None),
    "doctor": (None, None),
}


def _load(module: str, cmd: str, extra: str | None):
    """Import a subcommand's entry point, or explain what is missing.

    `pip install ciqkit` pulls in nothing at all — that is the point, the broker
    and leases are stdlib-only. But it means `ciqkit layout` on a base install
    used to die with a bare `ModuleNotFoundError: numpy` traceback, which reads
    like a broken package rather than "you have not chosen an extra yet".
    """
    import importlib
    try:
        return importlib.import_module(module).main
    except ImportError as exc:
        missing = getattr(exc, "name", None) or "a dependency"
        # Double quotes: the single-quoted form is a literal argument to pip in
        # cmd.exe and PowerShell, which then fails to parse the brackets.
        hint = (f"    pip install \"ciqkit[{extra}]\"\n" if extra else "")
        raise SystemExit(
            f"ciqkit {cmd}: needs {missing}, which this install does not have.\n"
            f"{hint}"
            f"  (`ciqkit doctor` lists what is installed and what each extra is "
            f"for.)") from exc


def _check_console_scripts() -> tuple[bool, list[str]]:
    from .doctor import check_console_scripts
    return check_console_scripts()


def _doctor(argv) -> int:
    """Print the environment report (see :mod:`ciqkit.doctor`)."""
    import argparse
    from . import doctor

    ap = argparse.ArgumentParser(
        prog="ciqkit doctor",
        description="Check that the environment can actually drive the simulator.")
    ap.add_argument("--project", default=None, metavar="DIR",
                    help="also check this Connect IQ project: manifest, "
                         "installed devices, developer key, build route, tests")
    args = ap.parse_args(argv)
    report = doctor.run(args.project)
    print(doctor.render(report), end="")
    return 0 if report["ok"] else 1


def _safe_stdio() -> None:
    """Never die on printing a character the console's code page lacks.

    Redirected to a file or a pipe on Windows, stdout is encoded in the ANSI
    code page (cp1252), and several messages carry "→", "✓" or a non-Latin
    path: an UnicodeEncodeError there turns a successful command into a
    traceback. Replace instead -- JSON output is ASCII-escaped anyway.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    _safe_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_USAGE, end="")
        return 0
    if argv[0] in ("-V", "--version"):
        print(f"ciqkit {__version__}")
        return 0

    cmd, rest = argv[0], argv[1:]
    if cmd not in _COMMANDS:
        print(f"ciqkit: unknown command {cmd!r}\n", file=sys.stderr)
        print(_USAGE, end="", file=sys.stderr)
        return 2
    if cmd == "doctor":
        return _doctor(rest)
    module, extra = _COMMANDS[cmd]
    return _load(module, cmd, extra)(rest)


if __name__ == "__main__":
    sys.exit(main())
