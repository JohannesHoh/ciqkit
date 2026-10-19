"""The sim storage journal: snapshot, rotate, reset, restore, crash pinning.

The simulator keys every app's storage on the upper-cased PRG name in one
shared folder, so a run inherits whatever the last same-named run left and a
retry overwrites the evidence of a crash. These tests pin the behaviour that
fixes both, against a fake simulator directory (conftest points
CIQKIT_SIM_APPS_DIR / CIQKIT_STORAGE_JOURNAL at scratch).
"""
import json
import os

import pytest

from ciqkit.sim import storage


CRASH = """---
Error: Symbol Not Found Error
Details: 'Failed invoking <symbol>'
Time: 2026-09-28T13:17:53Z
Filename: 'CAP-VENU3'
Appname: Trail Tracker
Stack:
"""

OTHER_APP_CRASH = CRASH.replace("CAP-VENU3", "SOMETHING-ELSE")


@pytest.fixture
def sim():
    root = storage.sim_apps_dir()
    for sub in ("DATA", "SETTINGS", "LOGS"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)

    def put(name, payload=b"x", settings=b"s"):
        for ext in (".DAT", ".IDX", ".IMT"):
            with open(os.path.join(root, "DATA", name + ext), "wb") as fh:
                fh.write(payload + ext.encode())
        with open(os.path.join(root, "SETTINGS", name + ".SET"), "wb") as fh:
            fh.write(settings)

    def log(text):
        with open(os.path.join(root, "LOGS", "CIQ_LOG.YML"), "a") as fh:
            fh.write(text)

    put.root, put.log = root, log
    return put


def _read(root, rel):
    with open(os.path.join(root, rel), "rb") as fh:
        return fh.read()


def test_storage_name_is_what_monkeydo_names_it():
    assert storage.storage_name("/x/bin/cap-venu3.prg") == "CAP-VENU3"
    assert storage.storage_name("TrailTrackerV3-tests.prg") == "TRAILTRACKERV3-TESTS"
    with pytest.raises(ValueError):
        storage.storage_name("../../etc/passwd prg")


def test_snapshot_copies_only_that_apps_files(sim, tmp_path):
    sim("CAP-VENU3", b"mine")
    sim("OTHER", b"theirs")
    prg = tmp_path / "cap-venu3.prg"
    prg.write_bytes(b"prg")
    e = storage.snapshot(str(prg), kind="capture", app="ba3")
    assert sorted(e["files"]) == ["DATA/CAP-VENU3.DAT", "DATA/CAP-VENU3.IDX",
                                  "DATA/CAP-VENU3.IMT", "SETTINGS/CAP-VENU3.SET"]
    assert not any("OTHER" in f for f in os.listdir(os.path.join(e["dir"], "DATA")))
    on_disk = json.load(open(os.path.join(e["dir"], "entry.json")))
    assert on_disk["kind"] == "capture" and on_disk["app"] == "ba3"
    assert on_disk["prg_sha256"] and on_disk["empty"] is False


def test_empty_storage_is_still_journaled(sim):
    e = storage.snapshot("fresh-venu3.prg", kind="test")
    assert e["empty"] is True and e["files"] == {}


def test_rotation_keeps_last_n_but_never_a_pinned_one(sim):
    sim("CAP-VENU3")
    first = storage.snapshot("cap-venu3.prg", kind="capture", keep=3)
    storage.pin(first, reason="crash")
    for _ in range(5):
        storage.snapshot("cap-venu3.prg", kind="capture", keep=3)
    snaps = storage.list_snapshots("CAP-VENU3")
    assert len([s for s in snaps if not s["pinned"]]) == 3
    assert first["id"] in [s["id"] for s in snaps]


def test_reset_fresh_removes_only_the_named_app(sim):
    sim("CAP-VENU3")
    sim("OTHER")
    r = storage.reset("cap-venu3.prg")
    assert len(r["removed"]) == 4
    assert storage.storage_files("CAP-VENU3") == []
    assert len(storage.storage_files("OTHER")) == 4


def test_fixture_is_renamed_to_the_target_prg(sim):
    sim("TRAILTRACKERV3", b"prod")
    fx = storage.snapshot("TrailTrackerV3.prg", kind="fixture")
    storage.reset("cap-venu3.prg", fx["dir"])
    assert _read(sim.root, "DATA/CAP-VENU3.DAT") == b"prod.DAT"
    assert _read(sim.root, "SETTINGS/CAP-VENU3.SET") == b"s"


def test_a_fixture_mixing_two_apps_is_refused(sim, tmp_path):
    for n in ("A", "B"):
        (tmp_path / "DATA").mkdir(exist_ok=True)
        (tmp_path / "DATA" / f"{n}.DAT").write_bytes(b"x")
    with pytest.raises(ValueError):
        storage.reset("cap-venu3.prg", str(tmp_path))


def test_restore_journals_what_it_replaces(sim):
    sim("CAP-VENU3", b"old")
    old = storage.snapshot("cap-venu3.prg", kind="capture")
    sim("CAP-VENU3", b"new")
    r = storage.restore(old["id"])
    assert _read(sim.root, "DATA/CAP-VENU3.DAT") == b"old.DAT"
    prev = storage.find_snapshot(r["previous"])
    assert prev["kind"] == "pre-restore"
    assert open(os.path.join(prev["dir"], "DATA/CAP-VENU3.DAT"), "rb").read() == b"new.DAT"


def test_restore_of_an_empty_snapshot_empties_storage(sim):
    empty = storage.snapshot("cap-venu3.prg", kind="capture")
    sim("CAP-VENU3")
    storage.restore(empty["id"])
    assert storage.storage_files("CAP-VENU3") == []


def test_check_pins_on_a_crash_for_this_app_only(sim, tmp_path):
    sim("CAP-VENU3", b"before")
    sim.log(CRASH.replace("CAP-VENU3", "OLD"))   # pre-existing entries are not ours
    e = storage.snapshot("cap-venu3.prg", kind="capture")
    sim.log(OTHER_APP_CRASH)
    assert storage.check(e)["crashed"] is False

    sim("CAP-VENU3", b"after")
    sim.log(CRASH)
    art = tmp_path / "artifacts"
    r = storage.check(e, pin_to=str(art))
    assert r["crashed"] and r["entries"][0]["error"] == "Symbol Not Found Error"
    pinned = r["pinned"]
    assert pinned.startswith(str(art))
    assert _read(pinned, "pre-run/DATA/CAP-VENU3.DAT") == b"before.DAT"
    assert _read(pinned, "post-run/DATA/CAP-VENU3.DAT") == b"after.DAT"
    assert b"Symbol Not Found" in _read(pinned, "crash-excerpt.yml")
    assert os.path.isfile(os.path.join(pinned, "CIQ_LOG.YML"))
    assert storage.find_snapshot(e["id"])["pinned"] is True


def test_a_crash_screen_without_a_log_entry_still_pins(sim):
    e = storage.snapshot("cap-venu3.prg", kind="capture")
    r = storage.check(e, crash_screen=True)
    assert r["crashed"] and os.path.isdir(r["pinned"])


def test_log_rotation_counts_the_new_file_as_new(sim):
    sim.log("x" * 400)
    e = storage.snapshot("cap-venu3.prg", kind="capture")
    os.remove(os.path.join(sim.root, "LOGS", "CIQ_LOG.YML"))
    sim.log(CRASH)
    assert storage.check(e)["crashed"]


def test_storage_run_resets_then_pins(sim, tmp_path):
    sim("CAP-VENU3", b"leftover")
    with storage.StorageRun("cap-venu3.prg", kind="capture", artifacts=str(tmp_path),
                            quiet=True) as run:
        assert storage.storage_files("CAP-VENU3") == []   # fresh start
        sim.log(CRASH)
    assert run.result["crashed"]
    assert _read(run.result["pinned"], "pre-run/DATA/CAP-VENU3.DAT") == b"leftover.DAT"


def test_storage_run_keep_inherits(sim):
    sim("CAP-VENU3", b"leftover")
    with storage.StorageRun("cap-venu3.prg", kind="capture", start="keep", quiet=True):
        assert len(storage.storage_files("CAP-VENU3")) == 4


def test_cli_snap_check_restore(sim, capsys):
    sim("CAP-VENU3", b"v1")
    assert storage.main(["snap", "cap-venu3.prg", "--kind", "test", "--start", "fresh"]) == 0
    sid = capsys.readouterr().out.strip()
    assert storage.storage_files("CAP-VENU3") == []
    sim.log(CRASH)
    assert storage.main(["check", sid]) == 3
    assert storage.main(["restore", "CAP-VENU3@latest"]) == 0
    assert _read(sim.root, "DATA/CAP-VENU3.DAT") == b"v1.DAT"
    assert storage.main(["list"]) == 0


def test_cold_loads_and_test_runs_default_to_fresh_storage():
    """Determinism is the default: a cold push must not boot on another run's
    leftovers unless the caller asks for `keep` or names a fixture."""
    import inspect

    from ciqkit.sim import broker, session
    assert inspect.signature(session.SimSession.load).parameters[
        "storage_start"].default == "fresh"
    assert 'a.get("storage", "fresh")' in inspect.getsource(broker.Broker._dispatch)
    assert '"test", "fresh"' in inspect.getsource(session.SimSession.run_tests)


# ── same app UUID => same storage (the simulator treats a push as an update) ──

UUID = "5a0c6a8e1b2c4d3e9f10aabbccddeeff"


def _fake_prg(path, uuid=UUID):
    """Minimal PRG: a head section, then the entry-point section holding the
    UUID after a 2-byte count -- the layout monkeyc writes (SDK 9.1.0)."""
    head = bytes.fromhex("d000d00d") + (33).to_bytes(4, "big") + b"\0" * 33
    entry = bytes.fromhex("6060c0de") + (38).to_bytes(4, "big") + b"\0\x01" + \
        bytes.fromhex(uuid) + b"\0" * 20
    path.write_bytes(head + entry + b"rest")
    return str(path)


def _install(sim, name, t=None):
    media = os.path.join(sim.root, "MEDIA")
    os.makedirs(media, exist_ok=True)
    p = os.path.join(media, name + ".PRG")
    open(p, "wb").write(b"encrypted")
    if t is not None:
        os.utime(p, (t, t))
    return p


def test_prg_uuid_reads_the_entry_section(tmp_path):
    assert storage.prg_uuid(_fake_prg(tmp_path / "a.prg")) == UUID
    (tmp_path / "junk.prg").write_bytes(b"nope")
    assert storage.prg_uuid(str(tmp_path / "junk.prg")) is None


def test_unknown_uuid_guesses_the_prg_name(sim, tmp_path):
    prg = _fake_prg(tmp_path / "mx-log-venu3.prg")
    assert storage.resolve_name(prg) == ("MX-LOG-VENU3", "prg-name")


def test_learned_install_name_wins_for_every_build_of_the_app(sim, tmp_path):
    import time
    first = _fake_prg(tmp_path / "cap-venu3.prg")
    t0 = time.time()
    _install(sim, "CAP-VENU3")
    assert storage.learn_installed(first, t0) == "CAP-VENU3"
    other = _fake_prg(tmp_path / "TrailTrackerV3-tests.prg")   # same UUID
    assert storage.resolve_name(other) == ("CAP-VENU3", "registry")
    # the installed app went away (Delete All Apps): back to guessing
    os.remove(os.path.join(sim.root, "MEDIA", "CAP-VENU3.PRG"))
    assert storage.resolve_name(other)[1] == "prg-name"


def test_after_push_reapplies_the_start_state_under_the_real_name(sim, tmp_path):
    import time
    sim("CAP-VENU3", b"test-junk")
    prg = _fake_prg(tmp_path / "mx-log-venu3.prg")
    run = storage.StorageRun(prg, kind="capture", quiet=True).begin()
    assert run.entry["storage_name"] == "MX-LOG-VENU3"
    t0 = time.time()
    _install(sim, "CAP-VENU3")                    # the push landed on the old install
    assert run.after_push(t0) is False            # -> caller pushes again
    assert run.entry["storage_name"] == "CAP-VENU3"
    assert storage.storage_files("CAP-VENU3") == []            # now fresh
    assert _read(run.entry["dir"], "DATA/CAP-VENU3.DAT") == b"test-junk.DAT"   # journaled
    assert run.after_push(time.time() + 5) is True    # nothing new installed: fine


def test_a_crash_logged_under_the_installed_name_is_found(sim, tmp_path):
    import time
    prg = _fake_prg(tmp_path / "mx-log-venu3.prg")
    e = storage.snapshot(prg, kind="capture")
    _install(sim, "CAP-VENU3", t=time.time() + 1)
    sim.log(CRASH)                                 # Filename: 'CAP-VENU3'
    r = storage.check(e)
    assert r["crashed"] and r["installed_as"] == "CAP-VENU3"
    assert r["start_state_applied"] is False


def test_objstore_is_journaled_and_fixture_renamed(sim, tmp_path):
    sim("BA3")
    base = os.path.join(sim.root, "DATA", "MEDIA", "OBJSTORE", "BA3")
    os.makedirs(os.path.join(base, "comp"))
    open(os.path.join(base, "comp", "0001.cmp"), "wb").write(b"c1")
    open(os.path.join(base, "BA3.SEN"), "wb").write(b"s")
    fx = storage.snapshot("BA3", kind="fixture")
    assert "DATA/MEDIA/OBJSTORE/BA3/comp/0001.cmp" in fx["files"]
    storage.reset("CAP-VENU3", fx["dir"])
    assert _read(sim.root, "DATA/MEDIA/OBJSTORE/CAP-VENU3/comp/0001.cmp") == b"c1"
    assert _read(sim.root, "DATA/MEDIA/OBJSTORE/CAP-VENU3/CAP-VENU3.SEN") == b"s"
    storage.reset("CAP-VENU3")
    assert not os.path.exists(os.path.join(sim.root, "DATA", "MEDIA", "OBJSTORE", "CAP-VENU3"))


def test_reapply_restores_the_start_state_without_a_new_entry(sim):
    run = storage.StorageRun("cap-venu3.prg", kind="capture", quiet=True).begin()
    sim("CAP-VENU3", b"written-by-attempt-1")
    n = len(storage.list_snapshots("CAP-VENU3"))
    run.reapply()
    assert storage.storage_files("CAP-VENU3") == []
    assert len(storage.list_snapshots("CAP-VENU3")) == n
