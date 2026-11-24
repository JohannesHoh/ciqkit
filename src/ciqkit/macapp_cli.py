"""``ciqkit mac-app``: give the broker its own name in macOS' privacy settings."""
from __future__ import annotations

import argparse
import sys

from . import macapp

_NEXT = ("System Settings > Privacy & Security: switch on ciqkit under Accessibility "
         "and under Screen & System Audio Recording.")


def _ask_broker(request: bool) -> dict:
    """What macOS allows the broker; {} if the running one is not the app's."""
    from .sim.client import BrokerNotRunning, Client
    try:
        if not Client(autospawn=False).ping().get("as_app"):
            print("mac-app: the running broker was started without the app. Run "
                  "`ciqkit sim shutdown` when it is idle, then this again.", file=sys.stderr)
            return {}
    except (BrokerNotRunning, OSError):
        pass
    return Client().preflight(request=request)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ciqkit mac-app", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="build ~/Applications/ciqkit.app")
    p.add_argument("--python", default=None, help="interpreter that runs the broker")
    p.add_argument("--force", action="store_true",
                   help="rebuild even if unchanged (macOS then forgets its permissions)")
    sub.add_parser("status", help="show the app and what macOS allows it")
    sub.add_parser("request", help="have macOS list ciqkit in its privacy settings")
    sub.add_parser("uninstall", help="remove the app")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "install":
            from .sim.client import resolve_broker_python
            python = args.python or resolve_broker_python()
            if not python:
                raise macapp.AppError("no interpreter with ciqkit[sim] found; pass --python")
            path, rebuilt = macapp.install(python, force=args.force)
            print(f"{'built' if rebuilt else 'already up to date'}: {path}")
            if rebuilt:
                print(_NEXT + "\nRun `ciqkit mac-app request` to have it listed there.")
            return 0
        if args.cmd == "uninstall":
            print("removed" if macapp.uninstall() else "not installed")
            return 0
        if not macapp.installed_for_me():
            print("ciqkit.app is not installed for this ciqkit; run `ciqkit mac-app install`")
            return 1
        print(f"{macapp.app_path()}\n  runs {macapp.info()['CIQKitPython']}")
        r = _ask_broker(args.cmd == "request")
        if not r:
            return 1
        ok = True
        for name in ("accessibility", "screen_recording"):
            ok = ok and r.get(name) is not False
            print(f"  {name}: " + {True: "allowed", False: "NOT allowed"}.get(
                r.get(name), "unknown"))
        if not ok:
            print(_NEXT)
        return 0 if ok else 1
    except macapp.AppError as exc:
        print(f"mac-app: {exc}", file=sys.stderr)
        return 1
