"""``ciqkit lease`` -- run a command while holding a shared-resource lease.

Shell entry points wrap their critical section in this so every concurrent
session, human or agent, serialises on the machine-wide singletons
automatically. Python callers import :mod:`ciqkit.leases` directly instead.

Usage::

    ciqkit lease <resource> [--timeout S] [--who LABEL] -- <cmd> [args...]
    ciqkit lease status
    ciqkit lease release <resource>

``<resource>`` is any string; ciqkit itself uses ``sim`` for the simulator and
``build:<project>`` for a project's build directory.

Exit codes:
    the command's own exit code on success
    75  (EX_TEMPFAIL) if the lease could not be acquired within --timeout
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys

from . import leases as leaselib
from ._compat import IS_WIN


def _run_child(cmd) -> int:
    """Run `cmd` in its OWN process group, forwarding SIGTERM/SIGINT to the whole
    group, then wait for it. Two problems this solves:

    1. Without forwarding, a SIGTERM to this wrapper would kill the Python
       process immediately and ORPHAN the child. A build script that mutates
       files and relies on an EXIT/INT/TERM trap to restore them would then
       never run its cleanup, leaving the working tree modified.
    2. Signalling only the direct child is not enough: a shell defers its trap
       until the foreground command returns, so a build script blocked in a
       long `monkeyc` would ignore the signal until the compile finished.
       Signalling the whole process group also terminates the compiler, which
       unblocks the script so its cleanup trap runs promptly.

    (SIGKILL is uncatchable and cannot be forwarded.)"""
    if IS_WIN:
        return _run_child_windows(cmd)
    proc = subprocess.Popen(cmd, start_new_session=True)  # child leads a new pgrp

    def _forward(signum, _frame):
        try:
            os.killpg(os.getpgid(proc.pid), signum)
        except Exception:
            try:
                proc.send_signal(signum)
            except Exception:
                pass  # child already gone

    prev = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            prev[sig] = signal.signal(sig, _forward)
        except (ValueError, OSError):
            pass  # not on the main thread / unsupported — best effort
    try:
        while True:
            try:
                return proc.wait()
            except KeyboardInterrupt:
                # SIGINT already forwarded to the group by the handler; keep
                # waiting for the child to run its trap and exit.
                continue
    finally:
        for sig, handler in prev.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


def _resolve_windows_argv(cmd):
    """Make ``cmd[0]`` something CreateProcess can start.

    CreateProcess only searches PATH for ``.exe``; ``npm``, ``gradle`` and
    every other ``.cmd``/``.bat`` shim on PATH fail with "file not found"
    unless they are spelled out. And a POSIX shell script -- the
    ``./scripts/test.sh`` this command is usually wrapped around -- is run by
    Git for Windows' bash, never by the ``bash.exe`` WSL puts on PATH, which
    would run it in a different filesystem and a different OS.
    """
    import shutil
    first = cmd[0]
    if first.lower().endswith(".sh"):
        from .winbash import git_bash
        bash = git_bash()
        if bash:
            return [bash, first.replace("\\", "/"), *cmd[1:]]
        return cmd
    found = shutil.which(first)
    return [found, *cmd[1:]] if found else cmd


def _run_child_windows(cmd) -> int:
    """Windows has no process groups to signal and no SIGTERM to forward.

    The child is started in this console's own process group, so a Ctrl-C
    typed into the console reaches it directly -- the job forwarding does on
    POSIX happens by construction. Our job is only to keep waiting so the
    child can run its cleanup, instead of exiting and releasing the lease
    while it is still running.
    """
    proc = subprocess.Popen(_resolve_windows_argv(cmd))
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            continue


def _run(args) -> int:
    if args.resource == "status":
        held = leaselib.status()
        if not held:
            print("no leases held")
            return 0
        for h in held:
            flag = "" if h.get("alive") else "  (holder DEAD — lease is free)"
            print("{:<22} pid {:<8} {:<24} since {}{}".format(
                h.get("resource", "?"),
                h.get("pid", "?"),
                h.get("who", "?"),
                h.get("since", "?"),
                flag,
            ))
        return 0

    if not args.cmd:
        print("ciqkit lease: no command given (use -- <cmd>)", file=sys.stderr)
        return 2

    try:
        with leaselib.lease(args.resource, timeout=args.timeout,
                            meta={"who": args.who} if args.who else None):
            return _run_child(args.cmd)
    except leaselib.LeaseTimeout as exc:
        print(f"ciqkit lease: {exc}", file=sys.stderr)
        return 75  # EX_TEMPFAIL — caller may retry


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # `release <name>` — manual best-effort release (clears a stale holder
    # sidecar; refuses if the holder is still alive, naming the pid to kill).
    if argv and argv[0] == "release":
        if len(argv) < 2:
            print("usage: ciqkit lease release <name>", file=sys.stderr)
            return 2
        name = argv[1]
        freed, holder = leaselib.release(name)
        if freed:
            if holder:
                print(f"released '{name}' (stale holder {holder.get('who')} "
                      f"pid {holder.get('pid')} was already gone)")
            else:
                print(f"'{name}' was not held — nothing to release")
            return 0
        print(f"'{name}' is HELD by a LIVE process: {holder.get('who')} "
              f"pid {holder.get('pid')} since {holder.get('since')}", file=sys.stderr)
        kill = (f"taskkill /PID {holder.get('pid')}" if IS_WIN
                else f"kill {holder.get('pid')}")
        print(f"  a live lock can't be broken remotely — stop its holder: "
              f"{kill}", file=sys.stderr)
        return 1

    # Split on the first '--' so flags can appear in any order before it and
    # the command after it is taken verbatim (its own flags untouched).
    if "--" in argv:
        sep = argv.index("--")
        head, cmd = argv[:sep], argv[sep + 1:]
    else:
        head, cmd = argv, []

    p = argparse.ArgumentParser(prog="ciqkit lease", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("resource", help="lease name, or 'status'")
    p.add_argument("--timeout", type=float, default=leaselib.DEFAULT_TIMEOUT,
                   help="seconds to wait for the lease (default %(default)s)")
    p.add_argument("--who", default=None, help="holder label for status output")
    args = p.parse_args(head)
    args.cmd = cmd
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
