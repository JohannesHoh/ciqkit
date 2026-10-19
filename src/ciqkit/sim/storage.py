"""Forensic journal + deterministic reset for the simulator's app storage.

The Connect IQ Simulator keeps every installed app's persistent state in ONE
shared folder::

    $TMPDIR/com.garmin.connectiq/GARMIN/APPS/MEDIA/<NAME>.PRG             installed app
    $TMPDIR/com.garmin.connectiq/GARMIN/APPS/DATA/<NAME>.DAT|.IDX|.IMT   Application.Storage
    $TMPDIR/com.garmin.connectiq/GARMIN/APPS/DATA/MEDIA/OBJSTORE/<NAME>/  complications, sensors
    $TMPDIR/com.garmin.connectiq/GARMIN/APPS/SETTINGS/<NAME>.SET         Properties
    $TMPDIR/com.garmin.connectiq/GARMIN/APPS/LOGS/CIQ_LOG.YML             crash log ("Filename: <NAME>")

``<NAME>`` is NOT simply the pushed PRG's name. A push behaves like an app
update on a watch: if an app with the same **application UUID** is already
installed, the new PRG replaces it *under the installed name* and inherits its
storage. Measured on SDK 9.1.0 (2026-09-28): pushing a copy of ``stprobe.prg``
as ``stprobe2.prg`` read and wrote ``STPROBE``'s storage. So every build of
one app -- debug, capture, a before/after scratch tree, AND its ``-t`` unit
test PRG -- shares a single storage file, named after whichever of them was
installed first. Only a first install uses the upper-cased PRG basename.

Consequences, both of which have cost real debugging time:

* **Every run inherits whatever the last run of the same app left** -- another
  branch, another session's unit tests (which write junk on purpose).
* **The evidence is overwritten by the next run.** A retry that passes has
  already replaced the storage the failing run booted on.

This module fixes both without touching the simulator itself:

* :func:`resolve_name` maps a PRG to its storage name: the UUID (read from the
  PRG header) looked up in a registry learned from earlier pushes, else the
  PRG's own name. :func:`learn_installed` teaches the registry after a push
  (the installed ``MEDIA/<NAME>.PRG`` is rewritten by every push), and
  :meth:`StorageRun.after_push` re-applies the start state when the guess was
  wrong, so the caller can push once more.
* :func:`snapshot` copies an app's storage into a bounded, per-app journal
  (last ``keep`` runs, default 20) with a JSON entry: app, PRG + UUID, run
  kind, time, git sha of the tree the PRG came from, and a CIQ_LOG.YML mark.
* :func:`reset` makes a run start from a declared state: empty, or a fixture
  directory (a snapshot is a valid fixture), renamed to the target's name.
* :func:`pin` copies a snapshot + CIQ_LOG.YML (+ the PRG) next to a failing
  run's artifacts and marks it so rotation never deletes it.
* :class:`StorageRun` bundles snapshot -> reset -> run -> check/pin.

The simulator re-reads storage from disk on every app start (deleting the
files while the app runs, then pushing again, boots it empty), and a push
that replaces a running app does not write the old instance's state back.

Only files that belong to one storage name are ever read or written; other
apps' storage is never touched. Stdlib only, like the rest of the broker.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time

from .._compat import IS_WIN, hidden_console_kwargs, lock_blocking, open_lock_file, unlock

__all__ = ["sim_apps_dir", "storage_name", "storage_files", "journal_root",
           "prg_uuid", "resolve_name", "learn_installed",
           "log_mark", "new_log_text", "crash_entries", "snapshot", "reset",
           "restore", "pin", "list_snapshots", "find_snapshot", "StorageRun",
           "main"]

DEFAULT_KEEP = 20
# rel dir under APPS -> extensions that belong to one storage name there
_STORAGE_LAYOUT = (("DATA", (".DAT", ".IDX", ".IMT")), ("SETTINGS", (".SET",)))
# Relative paths in the journal are always "/"-separated, on Windows too: an
# entry.json is a portable record (pinned next to a failing run's shots and
# replayable on another machine), and os.path.join accepts "/" everywhere.
_OBJSTORE = "DATA/MEDIA/OBJSTORE"   # + /<NAME>/... per app
_REGISTRY = "installed.json"
_ENTRY = "entry.json"
_NAME_RE = re.compile(r"^[A-Z0-9_.-]+$")


# ── locations ───────────────────────────────────────────────────────────────
def _darwin_user_temp() -> str | None:
    """The simulator is a GUI app launched by launchd, so its $TMPDIR is the
    per-user Darwin temp dir -- NOT necessarily ours (agent harnesses often
    point TMPDIR at a sandbox). Ask the OS rather than trusting the env."""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return out or None
    except Exception:
        return None


def sim_apps_dir() -> str:
    """``.../com.garmin.connectiq/GARMIN/APPS`` (``CIQKIT_SIM_APPS_DIR`` overrides)."""
    env = os.environ.get("CIQKIT_SIM_APPS_DIR")
    if env:
        return env
    base = _darwin_user_temp() or tempfile.gettempdir()
    return os.path.join(base, "com.garmin.connectiq", "GARMIN", "APPS")


def ciq_log_path() -> str:
    return os.path.join(sim_apps_dir(), "LOGS", "CIQ_LOG.YML")


def storage_name(prg_or_name: str) -> str:
    """``/x/bin/cap-venu3.prg`` -> ``CAP-VENU3``: the name monkeydo gives a
    FIRST install. See :func:`resolve_name` for what a push really uses."""
    base = os.path.basename(prg_or_name.rstrip("/"))
    if base.lower().endswith(".prg"):
        base = base[:-4]
    name = base.upper()
    if not name or not _NAME_RE.match(name):
        raise ValueError(f"not a usable storage name: {prg_or_name!r}")
    return name


def _rel(path: str, start: str) -> str:
    return os.path.relpath(path, start).replace(os.sep, "/")


def storage_files(name: str, root: str | None = None) -> list[str]:
    """Existing storage files for ``name`` under ``root`` (sim dir by default),
    as paths RELATIVE to root, e.g. ``DATA/CAP-VENU3.DAT``."""
    root = root or sim_apps_dir()
    out = []
    for sub, exts in _STORAGE_LAYOUT:
        for ext in exts:
            rel = f"{sub}/{name}{ext}"
            if os.path.isfile(os.path.join(root, rel)):
                out.append(rel)
    obj = os.path.join(root, _OBJSTORE, name)
    if os.path.isdir(obj):
        for d, _dirs, files in os.walk(obj):
            for fn in sorted(files):
                out.append(_rel(os.path.join(d, fn), root))
    return out


# ── which name does a PRG's storage live under? ─────────────────────────────
_ENTRY_SECTION = bytes.fromhex("6060c0de")


def prg_uuid(prg: str) -> str | None:
    """The application UUID from a built (unencrypted) PRG, or None.

    A PRG is a list of ``[4-byte magic][4-byte big-endian length][data]``
    sections; the entry-point section (``60 60 c0 de``) starts with a 2-byte
    count followed by the 16-byte UUID of the first entry."""
    try:
        with open(prg, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    i = 0
    while i + 8 <= len(head):
        magic, length = head[i:i + 4], int.from_bytes(head[i + 4:i + 8], "big")
        if magic == _ENTRY_SECTION and length >= 18 and i + 26 <= len(head):
            return head[i + 10:i + 26].hex()
        if length <= 0:
            break
        i += 8 + length
    return None


def _registry_path() -> str:
    return os.path.join(journal_root(), _REGISTRY)


def _registry() -> dict:
    try:
        return _read_json(_registry_path())
    except (OSError, ValueError):
        return {}


def resolve_name(prg: str) -> tuple[str, str]:
    """(storage name, how it was decided) for pushing ``prg`` now.

    ``registry``: an app with this UUID was seen installed under that name and
    its ``MEDIA/<NAME>.PRG`` is still there. ``prg-name``: nothing known, so a
    first install is assumed. That guess can be wrong (another tool installed
    the app first); :meth:`StorageRun.after_push` catches and corrects it."""
    if not prg.lower().endswith(".prg"):
        return storage_name(prg), "name"
    uuid = prg_uuid(prg)
    if uuid:
        hit = _registry().get(uuid)
        if hit and os.path.isfile(os.path.join(sim_apps_dir(), "MEDIA", hit["name"] + ".PRG")):
            return hit["name"], "registry"
    return storage_name(prg), "prg-name"


def learn_installed(prg: str, since: float) -> str | None:
    """After pushing ``prg`` at ``since``: the installed name it landed under
    (the ``MEDIA/*.PRG`` the push rewrote), remembered for its UUID."""
    media = os.path.join(sim_apps_dir(), "MEDIA")
    try:
        cands = [(os.path.getmtime(os.path.join(media, f)), f) for f in os.listdir(media)
                 if f.upper().endswith(".PRG")]
    except OSError:
        return None
    cands = [c for c in cands if c[0] >= since - 1.0]
    if not cands:
        return None
    name = max(cands)[1][:-4].upper()
    uuid = prg_uuid(prg)
    if uuid:
        with _JournalLock():
            reg = _registry()
            reg[uuid] = {"name": name, "seen": _now_iso(), "prg": os.path.abspath(prg)}
            _write_json(_registry_path(), reg)
    return name


def journal_root() -> str:
    """Persistent (survives reboots, unlike $TMPDIR): the whole point is to
    still have the storage a crash booted on after the retry passed."""
    env = os.environ.get("CIQKIT_STORAGE_JOURNAL")
    if env:
        path = env
    elif IS_WIN and not os.environ.get("XDG_STATE_HOME"):
        # %LOCALAPPDATA%, not %TEMP%: Windows' storage-sense cleans temp, and
        # this has to outlive the retry that overwrote the evidence.
        local = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        path = os.path.join(local, "ciqkit", "storage-journal")
    else:
        state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
        path = os.path.join(state, "ciqkit", "storage-journal")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _keep() -> int:
    try:
        return max(1, int(os.environ.get("CIQKIT_STORAGE_JOURNAL_KEEP", DEFAULT_KEEP)))
    except ValueError:
        return DEFAULT_KEEP


# ── small helpers ───────────────────────────────────────────────────────────
def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _git_info(path: str | None) -> dict:
    """sha + dirty flag of the checkout ``path`` lives in; {} outside git."""
    if not path:
        return {}
    d = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
    try:
        top = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"],
                             capture_output=True, encoding="utf-8", errors="replace",
                             timeout=10, **hidden_console_kwargs())
        if top.returncode != 0:
            return {}
        sha = subprocess.run(["git", "-C", d, "rev-parse", "HEAD"],
                             capture_output=True, encoding="utf-8", errors="replace",
                             timeout=10, **hidden_console_kwargs()).stdout.strip()
        dirty = subprocess.run(["git", "-C", d, "status", "--porcelain",
                                "--untracked-files=no"],
                               capture_output=True, encoding="utf-8", errors="replace",
                               timeout=20, **hidden_console_kwargs()).stdout.strip()
        return {"root": top.stdout.strip(), "sha": sha, "dirty": bool(dirty)}
    except Exception:
        return {}


class _JournalLock:
    """Serialises rotation/pinning between concurrent processes."""

    def __init__(self):
        self._fd = None

    def __enter__(self):
        self._fd = open_lock_file(os.path.join(journal_root(), ".lock"))
        lock_blocking(self._fd)
        return self

    def __exit__(self, *exc):
        try:
            unlock(self._fd)
        finally:
            os.close(self._fd)


def _write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _read_json(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


# ── CIQ_LOG.YML ─────────────────────────────────────────────────────────────
def log_mark() -> dict:
    """Where CIQ_LOG.YML ends right now. The sim appends; when it rotates the
    file to CIQ_LOG.BAK the new file is shorter than the mark, or starts
    differently, and :func:`new_log_text` then returns all of it."""
    p = ciq_log_path()
    try:
        st = os.stat(p)
        with open(p, "rb") as fh:
            head = fh.read(256)
        return {"size": st.st_size, "mtime": st.st_mtime,
                "head": hashlib.sha256(head).hexdigest()[:16]}
    except FileNotFoundError:
        return {"size": 0, "mtime": 0, "head": ""}


def new_log_text(mark: dict | None) -> str:
    """CIQ_LOG.YML text appended since ``mark`` ('' if nothing changed)."""
    p = ciq_log_path()
    try:
        with open(p, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return ""
    if not mark:
        return data.decode("utf-8", "replace")
    size = int(mark.get("size", 0))
    rotated = (len(data) < size or
               (size and hashlib.sha256(data[:256]).hexdigest()[:16] != mark.get("head")
                and len(data) >= 256 and size >= 256))
    tail = data if rotated else data[size:]
    return tail.decode("utf-8", "replace")


def crash_entries(text: str, name: str | None = None) -> list[dict]:
    """Parse CIQ_LOG entries (``---``-separated YAML-ish blocks). With
    ``name``, only entries whose ``Filename`` is that storage name."""
    out = []
    for block in re.split(r"(?m)^---\s*$", text):
        if "Error:" not in block:
            continue
        fields = {}
        for key in ("Error", "Details", "Time", "Filename", "Appname"):
            m = re.search(rf"(?m)^{key}:\s*'?(.*?)'?\s*$", block)
            if m:
                fields[key.lower()] = m.group(1)
        fields["raw"] = block.strip("\n")
        if name and fields.get("filename", "").upper() != name:
            continue
        out.append(fields)
    return out


# ── journal ─────────────────────────────────────────────────────────────────
def _app_dir(name: str) -> str:
    d = os.path.join(journal_root(), name)
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def list_snapshots(name: str | None = None) -> list[dict]:
    """Journal entries, oldest first (all apps when ``name`` is None)."""
    root = journal_root()
    names = [name] if name else sorted(
        n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n))
        and _NAME_RE.match(n))
    out = []
    for n in names:
        d = os.path.join(root, n)
        if not os.path.isdir(d):
            continue
        for sid in sorted(os.listdir(d)):
            ep = os.path.join(d, sid, _ENTRY)
            if os.path.isfile(ep):
                try:
                    e = _read_json(ep)
                except (OSError, ValueError):
                    continue   # a half-written entry: skip it, rotation will too
                e["dir"] = os.path.join(d, sid)
                out.append(e)
    out.sort(key=lambda e: e.get("id", ""))
    return out


def find_snapshot(ref: str) -> dict:
    """Entry by id, id prefix, snapshot dir, or ``NAME@latest`` / ``NAME@-2``."""
    if os.path.isfile(os.path.join(ref, _ENTRY)):
        e = _read_json(os.path.join(ref, _ENTRY))
        e["dir"] = os.path.abspath(ref)
        return e
    if "@" in ref:
        name, which = ref.split("@", 1)
        snaps = list_snapshots(storage_name(name))
        idx = -1 if which in ("", "latest") else int(which)
        if not snaps:
            raise LookupError(f"no snapshots for {name}")
        return snaps[idx]
    hits = [e for e in list_snapshots() if e.get("id", "").startswith(ref)]
    if len(hits) != 1:
        raise LookupError(f"{len(hits)} snapshots match {ref!r}")
    return hits[0]


def _rotate(name: str, keep: int) -> list[str]:
    snaps = [e for e in list_snapshots(name) if not e.get("pinned")]
    doomed = snaps[:-keep] if len(snaps) > keep else []
    for e in doomed:
        shutil.rmtree(e["dir"], ignore_errors=True)
    return [e["id"] for e in doomed]


def snapshot(prg: str, *, kind: str, app: str | None = None,
             note: str | None = None, keep: int | None = None,
             name: str | None = None) -> dict:
    """Copy ``prg``'s current sim storage into the journal. Returns the entry
    (with ``dir``). An app with no storage yet still gets an entry -- "it booted
    on empty storage" is evidence too. ``name`` overrides :func:`resolve_name`."""
    how = "given"
    if name is None:
        name, how = resolve_name(prg)
    src = sim_apps_dir()
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    sid = f"{stamp}-{re.sub(r'[^a-z0-9_-]', '', kind.lower()) or 'run'}-{os.getpid()}"
    with _JournalLock():
        d = os.path.join(_app_dir(name), sid)
        os.makedirs(d, mode=0o700)
        files = {}
        for rel in storage_files(name, src):
            dst = os.path.join(d, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(src, rel), dst)
            files[rel] = {"size": os.path.getsize(dst), "sha256": _sha256(dst)}
        prg_abs = os.path.abspath(prg) if prg.lower().endswith(".prg") else None
        entry = {
            "id": sid, "storage_name": name, "name_source": how, "app": app,
            "kind": kind, "time": _now_iso(), "note": note, "pinned": False,
            "pinned_to": [], "prg": prg_abs,
            "prg_uuid": prg_uuid(prg_abs) if prg_abs else None,
            "prg_sha256": _sha256(prg_abs) if prg_abs and os.path.isfile(prg_abs) else None,
            "git": _git_info(prg_abs), "files": files, "empty": not files,
            "ciq_log": log_mark(), "pid": os.getpid(), "t": time.time(),
        }
        _write_json(os.path.join(d, _ENTRY), entry)
        _rotate(name, keep or _keep())
    entry["dir"] = d
    return entry


def _fixture_files(fixture: str) -> tuple[set, list[tuple[str, str]]]:
    """(source storage names, [(fixture rel path, target rel path with NAME)])"""
    names, pairs = set(), []
    for sub, exts in _STORAGE_LAYOUT:
        sd = os.path.join(fixture, sub)
        if not os.path.isdir(sd):
            continue
        for fn in sorted(os.listdir(sd)):
            stem, ext = os.path.splitext(fn)
            if ext.upper() in exts and os.path.isfile(os.path.join(sd, fn)):
                names.add(stem.upper())
                pairs.append((f"{sub}/{fn}", f"{sub}/{{NAME}}{ext.upper()}"))
    od = os.path.join(fixture, _OBJSTORE)
    if os.path.isdir(od):
        for app_dir in sorted(os.listdir(od)):
            base = os.path.join(od, app_dir)
            if not os.path.isdir(base):
                continue
            names.add(app_dir.upper())
            for d, _dirs, files in os.walk(base):
                for fn in sorted(files):
                    rel_in = _rel(os.path.join(d, fn), base)
                    stem, ext = os.path.splitext(fn)
                    if stem.upper() == app_dir.upper():   # e.g. <NAME>.SEN
                        rel_in = posixpath.join(posixpath.dirname(rel_in), "{NAME}" + ext)
                    pairs.append((_rel(os.path.join(d, fn), fixture),
                                  posixpath.join(_OBJSTORE, "{NAME}", rel_in)))
    return names, pairs


def reset(prg: str, fixture: str | None = None, name: str | None = None) -> dict:
    """Make ``prg``'s sim storage exactly ``fixture`` (a dir laid out like a
    snapshot: ``DATA/<ANY>.DAT`` ..., one app), or empty when None. Fixture
    files are renamed to the target storage name (``name``, default
    :func:`resolve_name`). Call it before the push: the simulator reads
    storage from disk when the app starts."""
    if name is None:
        name, _how = resolve_name(prg)
    root = sim_apps_dir()
    removed, written = [], []
    for rel in storage_files(name, root):
        os.remove(os.path.join(root, rel))
        removed.append(rel)
    obj = os.path.join(root, _OBJSTORE, name)
    if os.path.isdir(obj):
        shutil.rmtree(obj)
    if fixture:
        src_names, pairs = _fixture_files(fixture)
        if len(src_names) > 1:
            raise ValueError(f"fixture {fixture} mixes storage names {sorted(src_names)}; "
                             f"a fixture must hold one app's files")
        if not pairs:
            raise ValueError(f"fixture {fixture} holds no DATA/*.DAT|IDX|IMT or "
                             f"SETTINGS/*.SET files")
        for src_rel, dst_tpl in pairs:
            dst_rel = dst_tpl.replace("{NAME}", name)
            os.makedirs(os.path.dirname(os.path.join(root, dst_rel)), exist_ok=True)
            shutil.copyfile(os.path.join(fixture, src_rel), os.path.join(root, dst_rel))
            written.append(dst_rel)
    return {"storage_name": name, "removed": removed, "written": written,
            "fixture": fixture}


def restore(ref: str, prg: str | None = None) -> dict:
    """Put a snapshot back into the sim (as ``prg``'s name, default its own),
    journaling the storage it replaces first so a restore is never lossy."""
    e = find_snapshot(ref)
    target = prg or e["storage_name"]
    name = resolve_name(target)[0]
    before = snapshot(target, kind="pre-restore", note=f"before restoring {e['id']}",
                      name=name)
    r = reset(target, None if e.get("empty") else e["dir"], name=name)
    r.update({"snapshot": e["id"], "previous": before["id"]})
    return r


def pin(entry: dict, dest: str | None = None, *, reason: str = "",
        log_text: str = "") -> str:
    """Copy snapshot ``entry`` + CIQ_LOG.YML (+ the PRG) into ``dest`` (default:
    ``<journal>/pinned/<id>``) and mark it exempt from rotation. Returns the
    directory written."""
    dest = dest or os.path.join(journal_root(), "pinned", entry["id"])
    out = os.path.join(dest, "sim-storage-" + entry["id"]) if not dest.endswith(entry["id"]) else dest
    with _JournalLock():
        if os.path.isdir(out):
            shutil.rmtree(out)
        shutil.copytree(entry["dir"], os.path.join(out, "pre-run"))
        lp = ciq_log_path()
        if os.path.isfile(lp):
            shutil.copy2(lp, os.path.join(out, "CIQ_LOG.YML"))
        bak = lp[:-4] + ".BAK"
        if os.path.isfile(bak):
            shutil.copy2(bak, os.path.join(out, "CIQ_LOG.BAK"))
        if log_text:
            with open(os.path.join(out, "crash-excerpt.yml"), "w") as fh:
                fh.write(log_text)
        prg = entry.get("prg")
        if prg and os.path.isfile(prg):
            shutil.copy2(prg, os.path.join(out, os.path.basename(prg)))
        # storage as the run left it -- the "after" half of the evidence
        name = entry["storage_name"]
        for rel in storage_files(name):
            dst = os.path.join(out, "post-run", rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(sim_apps_dir(), rel), dst)
        _write_json(os.path.join(out, "PIN.json"), {
            "snapshot": entry["id"], "storage_name": name, "reason": reason,
            "pinned_at": _now_iso(), "app": entry.get("app"), "git": entry.get("git"),
            "replay": f"ciqkit storage restore {out}/pre-run  "
                      f"# then push {os.path.basename(prg) if prg else name.lower() + '.prg'}"})
        ep = os.path.join(entry["dir"], _ENTRY)
        if os.path.isfile(ep):
            e = _read_json(ep)
            e["pinned"] = True
            e.setdefault("pinned_to", []).append(out)
            _write_json(ep, e)
    return out


def check(entry: dict, *, crash_screen: bool = False, pin_to: str | None = None,
          reason: str = "") -> dict:
    """Did the run started at ``entry`` crash? Reads what CIQ_LOG.YML gained
    since the snapshot; pins on a hit for this app, or when the caller saw a
    crash screen itself (a crash screen with no log entry is still a crash)."""
    text = new_log_text(entry.get("ciq_log"))
    names = {entry["storage_name"]}
    actual = None
    if entry.get("prg") and entry.get("t"):
        # the run may have landed under another installed name (same UUID);
        # its crash is logged under THAT name
        try:
            actual = learn_installed(entry["prg"], entry["t"])
        except Exception:  # noqa: BLE001
            actual = None
        if actual:
            names.add(actual)
    mine = [m for m in crash_entries(text) if m.get("filename", "").upper() in names]
    crashed = bool(mine) or crash_screen
    res = {"crashed": crashed, "entries": [{k: v for k, v in m.items() if k != "raw"}
                                           for m in mine],
           "crash_screen": crash_screen, "pinned": None,
           "installed_as": actual,
           "start_state_applied": actual in (None, entry["storage_name"])}
    if crashed:
        why = reason or ("; ".join(f"{m.get('error')}" for m in mine) or "crash screen")
        res["pinned"] = pin(entry, pin_to, reason=why,
                            log_text="\n---\n".join(m["raw"] for m in mine))
    return res


class StorageRun:
    """snapshot -> declared start state -> run -> check, in one object::

        with StorageRun(prg, kind="capture", app="ba3", artifacts=out_dir,
                        start="fresh") as run:
            push_and_shoot()
            run.saw_crash_screen()      # optional, from your own detector
        # on exit: CIQ_LOG checked, snapshot pinned into out_dir on a crash

    ``start``: ``"fresh"`` (empty storage), ``"keep"`` (inherit; still
    journaled), or a fixture directory. Never raises from the journaling
    itself -- forensics must not be the thing that fails a capture.
    """

    def __init__(self, prg: str, *, kind: str, app: str | None = None,
                 artifacts: str | None = None, start: str = "fresh",
                 quiet: bool = False):
        self.prg, self.kind, self.app = prg, kind, app
        self.artifacts, self.start, self.quiet = artifacts, start, quiet
        self.entry: dict | None = None
        self.result: dict | None = None
        self._crash_screen = False

    def _say(self, msg: str) -> None:
        if not self.quiet:
            print(f"[sim-storage] {msg}", file=sys.stderr)

    def begin(self, name: str | None = None) -> StorageRun:
        try:
            self.entry = snapshot(self.prg, kind=self.kind, app=self.app, name=name)
            if self.start != "keep":
                r = reset(self.prg, None if self.start == "fresh" else self.start,
                          name=self.entry["storage_name"])
                self._say(f"{r['storage_name']}: journaled {self.entry['id']} "
                          f"({len(self.entry['files'])} files), start="
                          f"{'fresh' if self.start == 'fresh' else self.start}")
        except Exception as exc:  # noqa: BLE001
            if self.start not in ("fresh", "keep"):
                raise   # a declared fixture that failed to apply is a real error
            self._say(f"journal/reset skipped: {exc}")
        return self

    def reapply(self) -> None:
        """Put the declared start state back (no new journal entry): before a
        retry push, whose predecessor may already have written storage."""
        if self.entry is not None and self.start != "keep":
            reset(self.prg, None if self.start == "fresh" else self.start,
                  name=self.entry["storage_name"])

    def after_push(self, since: float) -> bool:
        """Call once the pushed app is up. Learns the installed name the push
        really landed under; if that is not the name the start state was
        applied to, journals + resets THAT storage and returns False -- push
        again, and this run then starts from the declared state. True when
        the guess was right (or nothing could be learned)."""
        if self.entry is None:
            return True
        try:
            actual = learn_installed(self.prg, since)
        except Exception:  # noqa: BLE001
            return True
        if not actual or actual == self.entry["storage_name"]:
            return True
        self._say(f"{os.path.basename(self.prg)} is installed as {actual}, not "
                  f"{self.entry['storage_name']} (same app UUID): re-applying start state")
        self.begin(name=actual)
        return False

    def saw_crash_screen(self) -> None:
        self._crash_screen = True

    def finish(self, reason: str = "") -> dict | None:
        if self.entry is None or self.result is not None:
            return self.result
        try:
            self.result = check(self.entry, crash_screen=self._crash_screen,
                                pin_to=self.artifacts, reason=reason)
            if self.result["crashed"]:
                self._say(f"CRASH -> pre-run storage + CIQ_LOG pinned at "
                          f"{self.result['pinned']}")
        except Exception as exc:  # noqa: BLE001
            self._say(f"crash check skipped: {exc}")
        return self.result

    def __enter__(self):
        return self.begin()

    def __exit__(self, *exc):
        self.finish()
        return False


# ── CLI ─────────────────────────────────────────────────────────────────────
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
