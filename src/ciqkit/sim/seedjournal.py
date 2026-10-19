"""Crash-safe record of the files a seed has patched.

:meth:`ciqkit.sim.session.SimSession.seed` edits files in your project and puts
them back when the job ends. That restore is driven from an in-memory backup,
which covers every exit the process can observe -- normal completion, failure,
``SIGTERM``, ``SIGINT``, ``atexit``. It cannot cover ``SIGKILL``, a power cut,
or the OOM killer: the process dies with the only copy of your original bytes
in its heap.

This module is the on-disk half. Before a seed touches anything, the original
bytes of every file it will patch are written to a private runtime directory,
together with a small JSON journal naming the owning pid and the digest of the
content the seed is about to write. If that process dies without restoring, the
next broker start -- or ``ciqkit sim restore`` -- finds the journal, sees the
pid is gone, and puts the files back.

The digest is what makes automatic recovery safe. Recovery only writes the
original back over a file that still holds exactly the patched bytes. If
anything else has touched the file since -- the user fixed it by hand, a
checkout restored it, they edited it for an unrelated reason -- the entry is
kept for a human instead. Reverting a leftover patch must never cost somebody
an edit they made themselves.

One journal per user at a time: there is one simulator, one broker, and one
active seed.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

from .._compat import pid_alive
from ..paths import state_dir

__all__ = ["record", "clear", "pending", "recover", "stale", "owner_alive",
           "journal_path"]

_JOURNAL = "journal.json"


def _dir() -> str:
    return state_dir("seed-backups")


def journal_path() -> str:
    return os.path.join(_dir(), _JOURNAL)


def _pid_alive(pid) -> bool:
    if not pid:            # 0 or None: nobody owns this journal
        return False
    # Not os.kill(pid, 0): on Windows a dead owner reads as alive, so a
    # SIGKILLed seed would never be recovered (see _compat.pid_alive).
    return pid_alive(pid)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_journal(pid, files) -> str:
    jp = journal_path()
    tmp = jp + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"pid": pid,
                   "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "files": files}, fh)
    os.replace(tmp, jp)   # atomic: a reader never sees a half-written journal
    return jp


def record(entries) -> str:
    """Persist ``[(path, original_bytes, patched_bytes), ...]`` before writing.

    Returns the journal path. Best-effort by design: if the runtime directory
    is unwritable the seed still proceeds with its in-memory backup, because
    losing crash recovery is not a reason to fail a capture.
    """
    d = _dir()
    # Anything an earlier recovery could not resolve is still waiting for a
    # human — carry it forward rather than dropping it on the next seed.
    files = [e for e in (pending() or {}).get("files", []) if e.get("kept")]
    kept_paths = {e.get("path") for e in files}
    for path, original, patched in entries:
        blob = os.path.join(d, _sha(path.encode())[:32] + ".bak")
        if path in kept_paths:
            # A stale entry for this very file; the fresh backup supersedes it.
            files = [e for e in files if e.get("path") != path]
        with open(blob, "wb") as fh:
            fh.write(original)
        files.append({"path": path, "backup": blob,
                      "original_sha256": _sha(original),
                      "patched_sha256": _sha(patched)})
    return _write_journal(os.getpid(), files)


def pending() -> dict | None:
    """The current journal, or None. Does not check whether its owner is alive."""
    try:
        with open(journal_path()) as fh:
            j = json.load(fh)
    except (OSError, ValueError):
        return None
    return j if isinstance(j, dict) and j.get("files") else None


def owner_alive() -> bool:
    """True while the process that wrote the journal is still running."""
    j = pending()
    return bool(j) and _pid_alive(j.get("pid"))


def clear() -> None:
    """Drop this process's entries after a successful in-memory restore.

    Entries a previous recovery flagged for a human survive: nothing has
    resolved them.
    """
    j = pending()
    if not j:
        return
    _settle(j, [e for e in j.get("files", []) if e.get("kept")])


def recover() -> list[str]:
    """Restore files left patched by a seed whose process is gone.

    Returns the paths put back (empty when there is nothing to do). Two things
    it deliberately will not do:

      * touch a journal whose owner is still running -- that belongs to a live
        broker, which restores from memory when its job ends. Yanking the files
        mid-job would make it build the source it is halfway through capturing;
      * overwrite a file that no longer holds the patched bytes. See
        :func:`stale`.
    """
    j = pending()
    if not j or _pid_alive(j.get("pid")):
        return []
    restored, kept = [], []
    for entry in j.get("files", []):
        path, blob = entry.get("path"), entry.get("backup")
        if not path or not blob:
            continue
        try:
            with open(blob, "rb") as fh:
                original = fh.read()
        except OSError:
            # No backup left to restore from, so there is nothing this entry can
            # ever do. Keeping it would nag through `ciqkit doctor` forever with
            # no action that could clear it.
            continue
        try:
            with open(path, "rb") as fh:
                current = fh.read()
        except OSError:
            kept.append(entry)   # unreadable right now — an unmounted volume,
            continue             # a moved project. The backup stays put.
        digest = _sha(current)
        if digest == entry.get("original_sha256"):
            continue             # already back to normal; nothing to do
        if digest != entry.get("patched_sha256"):
            kept.append(entry)   # changed since we patched it — hands off
            continue
        try:
            with open(path, "wb") as fh:
                fh.write(original)
            restored.append(path)
        except OSError:
            kept.append(entry)
    _settle(j, kept)
    return restored


def stale() -> list[dict]:
    """Entries :func:`recover` refused to write back, if any.

    Each carries ``path`` (the file ciqkit patched) and ``backup`` (the original
    bytes, still on disk) so a human can diff the two and decide. ``ciqkit
    doctor`` reports these.
    """
    j = pending()
    if not j or _pid_alive(j.get("pid")):
        return []
    return [e for e in j.get("files", []) if e.get("kept")]


def _settle(journal: dict, kept: list) -> None:
    """Delete the blobs of resolved entries; keep the ones a human must judge."""
    keep_ids = {id(e) for e in kept}
    for entry in journal.get("files", []):
        if id(entry) not in keep_ids:
            try:
                os.unlink(entry.get("backup", ""))
            except OSError:
                pass
    if not kept:
        try:
            os.unlink(journal_path())
        except OSError:
            pass
        return
    for e in kept:
        e["kept"] = True
    try:
        _write_journal(None, kept)   # pid None: nobody owns these
    except OSError:
        pass
