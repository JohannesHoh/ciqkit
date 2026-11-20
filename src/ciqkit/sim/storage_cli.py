"""``ciqkit storage`` CLI: the printing front-end over :mod:`ciqkit.sim.storage`."""
from __future__ import annotations

import argparse
import json
import sys

from .storage import (
    _keep,
    check,
    ciq_log_path,
    find_snapshot,
    journal_root,
    list_snapshots,
    pin,
    reset,
    restore,
    sim_apps_dir,
    snapshot,
    storage_name,
)

def _fmt(e: dict) -> str:
    size = sum(f["size"] for f in e.get("files", {}).values())
    g = e.get("git") or {}
    sha = (g.get("sha") or "")[:8] + ("+" if g.get("dirty") else "")
    return (f"{e['id']:<44} {e['storage_name']:<28} {e.get('kind') or '':<10} "
            f"{e.get('app') or '':<8} {sha:<10} {size:>9}B"
            f"{'  PINNED' if e.get('pinned') else ''}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="ciqkit storage",
        description="Journal, reset and replay the simulator's per-app storage.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("where", help="print the sim storage + journal paths")
    s = sub.add_parser("list", help="list journaled snapshots")
    s.add_argument("name", nargs="?", help="PRG path or storage name")
    s = sub.add_parser("show", help="print one snapshot entry")
    s.add_argument("ref")
    s = sub.add_parser("snap", help="journal PRG's storage (prints the id)")
    s.add_argument("prg")
    s.add_argument("--kind", default="manual")
    s.add_argument("--app")
    s.add_argument("--start", default="keep",
                   help="then reset: fresh | keep (default) | <fixture dir>")
    s = sub.add_parser("check", help="did the run since snapshot REF crash? "
                       "exit 3 + pin on a crash")
    s.add_argument("ref")
    s.add_argument("--pin-to")
    s.add_argument("--crash-screen", action="store_true")
    s.add_argument("--reason", default="")
    s = sub.add_parser("reset", help="empty PRG's storage, or load a fixture")
    s.add_argument("prg")
    s.add_argument("--fixture")
    s = sub.add_parser("restore", help="put a snapshot back (replay a crash)")
    s.add_argument("ref", help="id, id prefix, snapshot dir, NAME@latest, NAME@-2")
    s.add_argument("--as", dest="as_prg", help="restore under this PRG's name")
    s = sub.add_parser("pin", help="pin a snapshot into a directory")
    s.add_argument("ref")
    s.add_argument("--to")
    s.add_argument("--reason", default="manual")
    a = ap.parse_args(argv)

    if a.cmd == "where":
        print(f"sim storage: {sim_apps_dir()}\nciq log:     {ciq_log_path()}\n"
              f"journal:     {journal_root()}  (keep {_keep()} per app)")
        return 0
    if a.cmd == "list":
        snaps = list_snapshots(storage_name(a.name) if a.name else None)
        for e in snaps:
            print(_fmt(e))
        if not snaps:
            print("(no snapshots)")
        return 0
    if a.cmd == "show":
        print(json.dumps(find_snapshot(a.ref), indent=2, sort_keys=True))
        return 0
    if a.cmd == "snap":
        e = snapshot(a.prg, kind=a.kind, app=a.app)
        if a.start != "keep":
            reset(a.prg, None if a.start == "fresh" else a.start, name=e["storage_name"])
        print(f"sim-storage: {e['storage_name']} ({e['name_source']}), "
              f"{len(e['files'])} files journaled", file=sys.stderr)
        print(e["id"])
        return 0
    if a.cmd == "check":
        r = check(find_snapshot(a.ref), crash_screen=a.crash_screen,
                  pin_to=a.pin_to, reason=a.reason)
        if not r["start_state_applied"]:
            print(f"sim-storage: WARNING this PRG is installed as {r['installed_as']} "
                  f"(same app UUID), so the run started on THAT storage, not the "
                  f"declared start state. Learned; the next run gets it right.",
                  file=sys.stderr)
        if r["crashed"]:
            for m in r["entries"]:
                print(f"CRASH {m.get('error')} ({m.get('time')})", file=sys.stderr)
            print(f"pinned: {r['pinned']}", file=sys.stderr)
            return 3
        return 0
    if a.cmd == "reset":
        print(json.dumps(reset(a.prg, a.fixture), indent=2))
        return 0
    if a.cmd == "restore":
        print(json.dumps(restore(a.ref, a.as_prg), indent=2))
        return 0
    if a.cmd == "pin":
        print(pin(find_snapshot(a.ref), a.to, reason=a.reason))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
