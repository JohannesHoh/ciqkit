"""``ciqkit new``: start a Connect IQ project from a template."""
from __future__ import annotations

import argparse
import json
import sys

from . import scaffold


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="ciqkit new",
        description="Create a new Connect IQ project wired up for ciqkit: manifest "
                    "with a fresh application id, monkey.jungle, source, a unit-test "
                    "skeleton, resources, .mcp.json, CLAUDE.md and ciqkit.toml. "
                    "Creates a developer key if you have none.")
    ap.add_argument("name", help="project name (also the directory and the build output name)")
    ap.add_argument("--dir", default=".", metavar="PATH",
                    help="parent directory to create NAME in (default: current directory)")
    ap.add_argument("--template", default="blank", choices=scaffold.TEMPLATES)
    ap.add_argument("--type", default="app", choices=list(scaffold.TYPES), dest="type_",
                    help="app = watch-app (default), watchface, datafield")
    ap.add_argument("--devices", default=None, metavar="D1,D2",
                    help="comma-separated device ids (default: venu3 if installed)")
    args = ap.parse_args(argv)

    devices = [d.strip() for d in args.devices.split(",") if d.strip()] if args.devices else None
    try:
        res = scaffold.create(args.name, parent_dir=args.dir, template=args.template,
                              type_=args.type_, devices=devices)
    except (scaffold.ScaffoldError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for w in res.warnings:
        print(f"warning: {w}", file=sys.stderr)
    print(json.dumps({"ok": True, **res.to_dict()}))
    steps = [f"cd {res.project}",
             "ciqkit doctor --project .   # checks SDK, devices and key",
             "ciqkit sim test             # runs the (:test) unit tests",
             "ciqkit sim shot home --native   # builds, runs, captures the first screen"]
    print("next steps:\n  " + "\n  ".join(steps), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
