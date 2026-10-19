"""``ciqkit init``: set an existing Connect IQ project up for ciqkit."""
from __future__ import annotations

import argparse
import json
import sys

from . import init_cmd


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="ciqkit init",
        description="Add ciqkit to an existing Connect IQ project: ciqkit.toml, "
                    ".mcp.json servers, a marked CLAUDE.md block and .gitignore "
                    "entries. Prints the planned changes as a diff and changes "
                    "nothing unless --write is given. Existing files are never "
                    "overwritten: JSON is merged, text gets a marked block or "
                    "missing lines, an existing ciqkit.toml is left alone.")
    ap.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    ap.add_argument("--write", action="store_true", help="apply the changes")
    ap.add_argument("--ci", action="store_true",
                    help="also add .github/workflows/ciqkit-checks.yml")
    ap.add_argument("--json", action="store_true", help="machine-readable summary on stdout")
    args = ap.parse_args(argv)

    try:
        plan = init_cmd.plan(args.dir, ci=args.ci)
        written = init_cmd.write(plan) if args.write else []
    except (init_cmd.InitError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Exit 1 when a file could not be merged safely (invalid .mcp.json, a
    # symlink, broken CLAUDE.md markers): "nothing to change" would otherwise
    # read as "this project is wired up" when it is not.
    rc = 1 if plan.attention else 0
    if args.json:
        print(json.dumps({"ok": not plan.attention, "project": plan.project,
                          "write": args.write, "written": written, "notes": plan.notes,
                          "attention": [c.path for c in plan.attention],
                          "changes": [c.to_dict() for c in plan.changes]}))
        return rc

    for note in plan.notes:
        print(f"note: {note}")
    for c in plan.changes:
        if c.action == "skip":
            print(f"{'ATTENTION' if c.attention else 'skip':<7} {c.path}: {c.note}")
        else:
            print(f"{c.action:<7} {c.path}: {c.note}")
            if not args.write:
                print(c.diff(), end="" if c.diff().endswith("\n") else "\n")
    if args.write:
        if written or not plan.attention:
            print(f"\nwrote {len(written)} file(s)." if written else "\nnothing to change.")
    elif plan.pending:
        print("\ndry run: nothing was changed. Re-run with --write to apply.")
    elif not plan.attention:
        print("\nnothing to change.")
    if plan.attention:
        print(f"\n{len(plan.attention)} file(s) need manual attention (see ATTENTION "
              f"above); the project is not fully set up yet.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
