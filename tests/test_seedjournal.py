"""Crash recovery of the seed journal (``ciqkit.sim.seedjournal``).

A seed's in-memory backup dies with a SIGKILLed process; the on-disk journal is
what lets the next broker put the files back. "Dead process" here is a real,
reaped child pid judged by ``_compat.pid_alive`` -- never ``os.kill(pid, 0)``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from ciqkit import doctor
from ciqkit.sim import preflight, seedjournal

ORIGINAL = b"const SEED = 0;\r\nline two\n\x00\xff\n"
PATCHED = b"const SEED = 1;\r\nline two\n\x00\xfe\n"


@pytest.fixture
def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _kill_owner(pid: int) -> None:
    """Rewrite the journal as if its writer had been SIGKILLed."""
    jp = seedjournal.journal_path()
    with open(jp) as fh:
        j = json.load(fh)
    j["pid"] = pid
    with open(jp, "w") as fh:
        json.dump(j, fh)


def _seed(tmp_path, name="Seed.mc", original=ORIGINAL, patched=PATCHED) -> str:
    """Write the file already patched (as a seed leaves it) and journal it."""
    path = os.path.join(str(tmp_path), name)
    seedjournal.record([(path, original, patched)])
    with open(path, "wb") as fh:
        fh.write(patched)
    return path


def _read(path) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _doctor_seed_check() -> dict:
    checks: list = []
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(preflight, "detect_sdk", lambda: None)
        mp.setattr(preflight, "reset", lambda: None)
        mp.setattr(preflight, "check_once", lambda: {})
        mp.setattr(doctor, "check_console_scripts", lambda: (True, []))
        doctor._environment_checks(checks)
    finally:
        mp.undo()
    return next(c for c in checks if c["name"] == "seed") if any(
        c.get("name") == "seed" for c in checks) else {}


def test_nothing_recorded(tmp_path):
    assert seedjournal.pending() is None
    assert seedjournal.recover() == []
    assert seedjournal.stale() == []
    assert not seedjournal.owner_alive()


def test_live_owner_is_not_recovered(tmp_path):
    path = _seed(tmp_path)
    assert seedjournal.owner_alive()
    assert seedjournal.recover() == []
    assert _read(path) == PATCHED          # untouched while the owner lives
    assert seedjournal.pending() is not None


def test_dead_owner_restored_byte_exact(tmp_path, dead_pid):
    path = _seed(tmp_path)
    blobs = [e["backup"] for e in seedjournal.pending()["files"]]
    _kill_owner(dead_pid)
    assert not seedjournal.owner_alive()
    assert seedjournal.recover() == [path]
    assert _read(path) == ORIGINAL
    assert seedjournal.pending() is None
    assert not os.path.exists(seedjournal.journal_path())
    assert not any(os.path.exists(b) for b in blobs)


def test_zero_pid_counts_as_dead(tmp_path):
    path = _seed(tmp_path)
    _kill_owner(0)
    assert seedjournal.recover() == [path]
    assert _read(path) == ORIGINAL


def test_file_edited_since_is_kept_for_a_human(tmp_path, dead_pid):
    path = _seed(tmp_path)
    _kill_owner(dead_pid)
    with open(path, "wb") as fh:
        fh.write(b"user fixed it by hand\n")
    assert seedjournal.recover() == []
    assert _read(path) == b"user fixed it by hand\n"
    stale = seedjournal.stale()
    assert [e["path"] for e in stale] == [path]
    assert _read(stale[0]["backup"]) == ORIGINAL   # original bytes still on disk
    assert seedjournal.recover() == []             # idempotent, still kept


def test_already_original_is_not_rewritten(tmp_path, dead_pid):
    path = _seed(tmp_path)
    _kill_owner(dead_pid)
    with open(path, "wb") as fh:
        fh.write(ORIGINAL)
    assert seedjournal.recover() == []
    assert _read(path) == ORIGINAL
    assert seedjournal.pending() is None
    assert seedjournal.stale() == []


def test_missing_file_is_kept(tmp_path, dead_pid):
    path = _seed(tmp_path)
    _kill_owner(dead_pid)
    os.unlink(path)
    assert seedjournal.recover() == []
    assert [e["path"] for e in seedjournal.stale()] == [path]


def test_multiple_files_mixed_outcomes(tmp_path, dead_pid):
    a, b = os.path.join(str(tmp_path), "a.mc"), os.path.join(str(tmp_path), "b.mc")
    seedjournal.record([(a, b"a0", b"a1"), (b, b"b0", b"b1")])
    for p, data in ((a, b"a1"), (b, b"edited")):
        with open(p, "wb") as fh:
            fh.write(data)
    _kill_owner(dead_pid)
    assert seedjournal.recover() == [a]
    assert _read(a) == b"a0"
    assert [e["path"] for e in seedjournal.stale()] == [b]


def test_kept_entry_survives_clear_and_next_record(tmp_path, dead_pid):
    path = _seed(tmp_path)
    _kill_owner(dead_pid)
    with open(path, "wb") as fh:
        fh.write(b"mine\n")
    seedjournal.recover()
    seedjournal.clear()                    # a later in-memory restore
    assert [e["path"] for e in seedjournal.stale()] == [path]
    other = os.path.join(str(tmp_path), "other.mc")
    seedjournal.record([(other, b"o0", b"o1")])   # next seed carries it forward
    paths = {e["path"] for e in seedjournal.pending()["files"]}
    assert paths == {path, other}


def test_clear_after_normal_restore_removes_journal(tmp_path):
    path = _seed(tmp_path)
    blob = seedjournal.pending()["files"][0]["backup"]
    seedjournal.clear()
    assert seedjournal.pending() is None
    assert not os.path.exists(blob)
    assert _read(path) == PATCHED   # clear() only drops records, writes nothing


def test_missing_backup_blob_drops_entry(tmp_path, dead_pid):
    path = _seed(tmp_path)
    os.unlink(seedjournal.pending()["files"][0]["backup"])
    _kill_owner(dead_pid)
    assert seedjournal.recover() == []
    assert _read(path) == PATCHED
    assert seedjournal.pending() is None


def test_corrupt_journal_reads_as_none():
    with open(seedjournal.journal_path(), "w") as fh:
        fh.write("{not json")
    assert seedjournal.pending() is None
    assert seedjournal.recover() == []


def test_doctor_live_owner_is_info(tmp_path):
    _seed(tmp_path)
    c = _doctor_seed_check()
    assert c["status"] == "info"


def test_doctor_dead_owner_pending_is_warn(tmp_path, dead_pid):
    _seed(tmp_path)
    _kill_owner(dead_pid)
    assert _doctor_seed_check()["status"] == "warn"


def test_doctor_stale_is_fail_with_backup_path(tmp_path, dead_pid):
    path = _seed(tmp_path)
    _kill_owner(dead_pid)
    with open(path, "wb") as fh:
        fh.write(b"changed\n")
    seedjournal.recover()
    c = _doctor_seed_check()
    assert c["status"] == "fail"
    backup = seedjournal.stale()[0]["backup"]
    text = json.dumps(c)
    assert json.dumps(path)[1:-1] in text and json.dumps(backup)[1:-1] in text


def test_doctor_silent_after_recovery(tmp_path, dead_pid):
    _seed(tmp_path)
    _kill_owner(dead_pid)
    seedjournal.recover()
    assert _doctor_seed_check() == {}
