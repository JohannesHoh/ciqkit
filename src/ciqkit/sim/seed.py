"""Build-time seeding: patch a project's source for one job, put it back byte-exact.

:meth:`ciqkit.sim.session.SimSession.seed` and ``restore`` are thin delegates
to :func:`apply` and :func:`restore`; the session only holds the backup between
the two. None of this touches the GUI. :mod:`ciqkit.sim.seedjournal` is the
on-disk half that survives a SIGKILL.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess

from . import seedjournal
from .errors import SimError

log = logging.getLogger(__name__)


def apply(app_dir: str, consts: dict = None, patches: list = None,
          force: bool = False) -> tuple[list, dict]:
    """Write the seed; return ``(backups, result)``. Hand ``backups`` to
    :func:`restore`. See :meth:`SimSession.seed` for the full contract.
    """
    backups = []   # [(abs_path, original_bytes)], one per file, first read
    pending = {}   # abs_path -> text so far; written only once every check passed
    if consts:
        cpath = _seed_target(app_dir, os.path.join("source", "Constants.mc"))
        if not os.path.isfile(cpath):
            raise SimError(f"no Constants.mc at {cpath}")
        _refuse_if_dirty(cpath, force)
        with open(cpath, "rb") as fh:
            original = fh.read()
        backups.append((cpath, original))
        text = original.decode("utf-8")
        pending[cpath] = text
        for name, value in consts.items():
            pat = re.compile(rf"(const\s+{re.escape(name)}\s*=\s*)([^;]+)(;)")
            if not pat.search(text):
                raise SimError(f"const {name} not found in Constants.mc")
            # A function replacement, NOT an f-string template: in a
            # template, backslashes in the value are read as regular
            # expression escapes, so a value of `"\1"` would splice the
            # matched text back into the user's source and `"a\g"` would
            # raise. Seeds legitimately carry paths and escapes.
            text = pat.sub(
                lambda m, _v=value: m.group(1) + _v + m.group(3), text)
        pending[cpath] = text
    for p in (patches or []):
        fpath = _seed_target(app_dir, p["file"])
        if not os.path.isfile(fpath):
            raise SimError(f"patch target not found: {p['file']}")
        if fpath not in pending:
            _refuse_if_dirty(fpath, force)
            with open(fpath, "rb") as fh:
                orig = fh.read()
            backups.append((fpath, orig))
            pending[fpath] = orig.decode("utf-8")
        new_t, n = re.subn(p["pattern"], p["repl"], pending[fpath],
                           count=p.get("count", 1))
        if n == 0:
            raise SimError(f"patch pattern not found in {p['file']}: {p['pattern']}")
        pending[fpath] = new_t
    edits = [(path, text.encode("utf-8")) for path, text in pending.items()]

    # Every check has passed and nothing above this line touched the tree, so
    # a rejected seed can never half-apply. Journal the originals, then write.
    #
    # Writes are binary on purpose: the source was decoded as UTF-8, so it is
    # encoded back the same way rather than in whatever the locale happens to
    # be, and text mode on Windows would rewrite every newline. It also makes
    # the bytes on disk exactly what the journal hashed.
    originals = dict(backups)
    try:
        seedjournal.record([(p, originals[p], new) for p, new in edits])
    except OSError as exc:
        # Crash recovery is a safety net, not a precondition — but say so.
        log.warning("could not write the seed journal (%s); a SIGKILL during "
                    "this job would leave the patch in your tree", exc)
    try:
        for path, new_bytes in edits:
            with open(path, "wb") as fh:
                fh.write(new_bytes)
    except Exception:
        # A failed write mid-batch — roll every touched file back.
        for path, orig in backups:
            with open(path, "wb") as fh:
                fh.write(orig)
        seedjournal.clear()
        raise
    return backups, {"ok": True,
            "seeded_consts": list((consts or {}).keys()),
            "patched_files": [os.path.basename(f) for f, _ in backups
                              if not f.endswith("Constants.mc")]}


def restore(backups: list) -> dict:
    """Put every seeded file back byte-exact and report whether git sees no
    change. A write failure raises with the journal intact, so the caller keeps
    its backup and can retry."""
    restored, clean = [], True
    for path, original in backups:
        with open(path, "wb") as fh:
            fh.write(original)
        restored.append(path)
        clean = clean and _git_clean(path)
    seedjournal.clear()
    return {"ok": True, "restored": restored, "git_clean": clean}


def _seed_target(app_dir: str, rel: str) -> str:
    """Resolve a seed target inside ``app_dir``, refusing anything that escapes.

    ``os.path.join(app_dir, rel)`` is not a containment check: an absolute
    ``rel`` discards ``app_dir`` entirely, and ``../`` walks out of it. Seeding
    is reachable from the broker socket and from the MCP server, so an unchecked
    join is a write primitive for every file the user owns. Symlinks are
    resolved first, so a link inside the project cannot point out of it either.
    """
    root = os.path.realpath(app_dir)
    full = os.path.realpath(os.path.join(root, rel))
    if full != root and not full.startswith(root + os.sep):
        raise SimError(
            f"refusing to patch {rel!r}: it resolves to {full}, outside the "
            f"project directory {root}. Seed targets must be inside the project.")
    return full


def _git_status(path: str) -> str:
    """'clean' | 'dirty' | 'untracked' | 'no-git' for one file."""
    d = os.path.dirname(path) or "."
    try:
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", "--", path],
                                 cwd=d, capture_output=True)
        if tracked.returncode != 0:
            inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                                    cwd=d, capture_output=True)
            return "untracked" if inside.returncode == 0 else "no-git"
        r = subprocess.run(["git", "diff", "--quiet", "--", path],
                           cwd=d, capture_output=True)
        return "clean" if r.returncode == 0 else "dirty"
    except OSError:
        return "no-git"   # git is not installed; the journal still covers us


def _refuse_if_dirty(path: str, force: bool) -> None:
    """Do not patch a file whose uncommitted changes we could not give back.

    Restore is byte-exact and runs on every observable exit, but a SIGKILL
    leaves the patch behind. If the file was committed, that is a
    `git checkout` away; if it held uncommitted work, the journal is the only
    copy. Refusing by default keeps the worst case boring.
    """
    if force:
        return
    if _git_status(path) == "dirty":
        raise SimError(
            f"{path} has uncommitted changes. Seeding rewrites it and restores "
            f"it afterwards, but a SIGKILL mid-job would leave the patch in "
            f"place with your edits only in ciqkit's journal. Commit or stash "
            f"first, or pass force to seed anyway.")


def _git_clean(path: str) -> bool:
    return _git_status(path) in ("clean", "untracked", "no-git")
