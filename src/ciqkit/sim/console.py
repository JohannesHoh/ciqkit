"""Reading an app's console output (``System.println``) from monkeydo's log.

monkeydo stays attached to the simulator for as long as the pushed app runs and
streams the app's console to its stdout. The session sends that to a file and
keeps a cursor into it, so a marker is found once, in order, even when it was
printed before anyone started waiting for it. Pure functions here; the waiting
lives in :class:`ciqkit.sim.session.SimSession`.
"""
from __future__ import annotations

import re


def find_marker(data: bytes, start: int, marker: str, regex: bool = False):
    """The first COMPLETE line at or after byte offset ``start`` that contains
    ``marker`` (a substring, or a regular expression with ``regex``).

    Returns ``(line, end)`` -- the stripped line and the offset just past it, to
    be the next search's ``start`` -- or None. A trailing line with no newline
    yet is not matched: it may still be half-written.
    """
    pat = re.compile(marker) if regex else None
    pos = start
    while True:
        nl = data.find(b"\n", pos)
        if nl < 0:
            return None
        line = data[pos:nl].decode("utf-8", "replace").rstrip("\r")
        end = nl + 1
        if (pat.search(line) if pat else marker in line):
            return line.strip(), end
        pos = end


def tail(data: bytes, n: int = 8) -> str:
    """The last ``n`` lines, for an error message."""
    lines = data.decode("utf-8", "replace").splitlines()[-n:]
    return "\n".join(lines) if lines else "(the app printed nothing)"
