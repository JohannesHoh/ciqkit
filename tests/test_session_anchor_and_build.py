"""Two promises the simulator layer makes, checked without a simulator.

  * an OCR anchor CONFIRMS a result, it never gates one -- a machine with no
    OCR backend must still be able to drive the app;
  * a build hands back the .prg it just produced, never one that merely
    happened to be mentioned or lying around.
"""
import os
import time
import types

import pytest

from ciqkit.sim import session as session_mod
from ciqkit.sim.session import SimSession, SimError, _announced_prg


# --- anchors confirm, they do not gate ---------------------------------------

def _session_with_fake_screen(monkeypatch, changed: bool, ocr_result):
    """A SimSession whose capture/input/OCR are stubbed. Nothing touches a GUI."""
    s = SimSession.__new__(SimSession)          # no __init__: no SDK, no dirs
    s._hwnd, s._canvas, s.current = object(), object(), ("app", "venu3")
    s._warned_devices = set()
    monkeypatch.setattr(SimSession, "_capture", lambda self: object())
    monkeypatch.setattr(SimSession, "_face_sig", lambda self, img: object())
    monkeypatch.setattr(SimSession, "_frame_detail", lambda self, img: object())
    monkeypatch.setattr(SimSession, "_do_input",
                        lambda self, *a, **k: None)
    monkeypatch.setattr(session_mod.su, "dismiss_modal_dialogs",
                        lambda **k: None)
    monkeypatch.setattr(session_mod.su, "signatures_differ",
                        lambda a, b: changed)
    monkeypatch.setattr(session_mod.su, "details_differ",
                        lambda a, b: changed)
    monkeypatch.setattr(session_mod.ocr, "contains_any",
                        lambda img, anchors: ocr_result)
    monkeypatch.setattr(SimSession, "STABLE_S", 0)   # no pre-input settling here
    return s


def test_an_anchor_does_not_fail_a_gesture_when_ocr_is_unavailable(monkeypatch):
    """contains_any returns None with no backend. Treating that as "not found"
    made every anchored gesture fail outright on a machine without OCR --
    an anchor gating a result it was only meant to confirm."""
    s = _session_with_fake_screen(monkeypatch, changed=True, ocr_result=None)
    r = s.act("tap", anchor=["TOTAL"], retries=0, delay=0, settle_s=0)
    assert r["ok"] is True
    assert r["anchor"] is None          # reported as skipped, not as a miss


def test_a_matched_anchor_still_confirms(monkeypatch):
    s = _session_with_fake_screen(monkeypatch, changed=False, ocr_result=True)
    r = s.act("tap", anchor=["TOTAL"], retries=0, delay=0, settle_s=0)
    assert r["ok"] is True and r["anchor"] is True


def test_a_contradicted_anchor_still_fails_the_gesture(monkeypatch):
    """OCR ran and the expected text was NOT there: that is a real miss."""
    s = _session_with_fake_screen(monkeypatch, changed=True, ocr_result=False)
    r = s.act("tap", anchor=["TOTAL"], retries=0, delay=0, settle_s=0)
    assert r["ok"] is False


def test_an_unchanged_screen_still_fails_without_an_anchor(monkeypatch):
    s = _session_with_fake_screen(monkeypatch, changed=False, ocr_result=None)
    assert s.act("tap", retries=0, delay=0, settle_s=0)["ok"] is False


def test_a_local_change_the_coarse_test_misses_still_confirms(monkeypatch):
    """One mark placed on a dark game board moves the 4x4 average by ~1.5
    levels against a threshold of 12. Read as "no change", the gesture was
    retried -- and a retried START press is a second, real move."""
    s = _session_with_fake_screen(monkeypatch, changed=False, ocr_result=None)
    monkeypatch.setattr(session_mod.su, "details_differ", lambda a, b: True)
    calls = []
    monkeypatch.setattr(SimSession, "_do_input",
                        lambda self, *a, **k: calls.append(a))
    r = s.act("key", key="enter", retries=2, delay=0, settle_s=0)
    assert r["ok"] is True and r["changed"] is True
    assert len(calls) == 1, "a confirmed gesture was sent again"


def _counting_session(monkeypatch, frames_until_change: int):
    """act() sees "unchanged" for the first N captures after the input, then a
    change -- an app that is slow to redraw."""
    s = _session_with_fake_screen(monkeypatch, changed=False, ocr_result=None)
    seen = {"n": 0}

    def changed(self, before, after):
        seen["n"] += 1
        return seen["n"] > frames_until_change
    monkeypatch.setattr(SimSession, "_input_changed", changed)
    monkeypatch.setattr(SimSession, "SETTLE_POLL_S", 0.01)
    inputs = []
    monkeypatch.setattr(SimSession, "_do_input",
                        lambda self, *a, **k: inputs.append(a))
    return s, inputs


def test_a_slow_redraw_is_waited_for_not_resent(monkeypatch):
    """Judging "no change" on the first frame re-sent inputs that had landed:
    a second START press restarted a finished game."""
    s, inputs = _counting_session(monkeypatch, frames_until_change=3)
    r = s.act("tap", delay=0, settle_s=1.0)
    assert r["ok"] is True and r["attempts"] == 1
    assert len(inputs) == 1


def test_a_key_that_did_not_land_is_resent_after_the_settle_window(monkeypatch):
    """The first key after a cold load is genuinely dropped at times. A retry
    is safe once a full settle window showed no effect: the press did not land,
    so sending it again cannot act twice."""
    # settle_s=0: one check per attempt, so the count below cannot depend on
    # how many 10 ms polls a runner fits into a window (STABLE_S is 0 here, so
    # no baseline checks): check 1 is the first attempt, no change; check 2
    # the retry, changed.
    s, inputs = _counting_session(monkeypatch, frames_until_change=1)
    r = s.act("key", key="enter", delay=0, settle_s=0)
    assert r["ok"] is True and r["attempts"] == 2
    assert len(inputs) == 2


def test_retries_zero_sends_once(monkeypatch):
    s, inputs = _counting_session(monkeypatch, frames_until_change=10**6)
    assert s.act("key", key="enter", delay=0, settle_s=0, retries=0)["ok"] is False
    assert len(inputs) == 1


def test_the_baseline_waits_for_a_stable_frame(monkeypatch):
    """A baseline taken while the app is still drawing makes the next frame
    look changed whether or not the input landed."""
    s = _session_with_fake_screen(monkeypatch, changed=False, ocr_result=None)
    frames = iter(range(100))
    monkeypatch.setattr(SimSession, "_capture", lambda self: next(frames))
    monkeypatch.setattr(SimSession, "SETTLE_POLL_S", 0.0)
    monkeypatch.setattr(SimSession, "STABLE_S", 5.0)
    # Frames 0..2 are still settling; 2 -> 3 is the first unchanged pair.
    monkeypatch.setattr(SimSession, "_input_changed",
                        lambda self, a, b: not (a == 2 and b == 3))
    assert s._wait_stable() == 3


def test_every_kind_gets_its_retries(monkeypatch):
    s, inputs = _counting_session(monkeypatch, frames_until_change=10**6)
    assert s.act("tap", delay=0, settle_s=0)["ok"] is False
    assert len(inputs) == 3


# --- the build must return what it just built --------------------------------

def _prg(tmp_path, name, mtime):
    p = tmp_path / name
    p.write_bytes(b"prg")
    os.utime(p, (mtime, mtime))
    return p


def test_a_mentioned_but_stale_prg_is_not_taken_as_the_build_output(tmp_path):
    """Build scripts routinely name .prg files they did not just produce --
    an `rm -f bin/App-test.prg` line, a warning quoting an old binary. Taking
    the first one that merely exists is how last week's build, or a -t test
    binary, ends up in a screenshot."""
    started = time.time()
    _prg(tmp_path, "App-test.prg", started - 3600)     # stale, mentioned first
    fresh = _prg(tmp_path, "App.prg", started + 1)
    out = "rm -f App-test.prg\nBUILD SUCCESSFUL -> App.prg\n"
    assert _announced_prg(out, str(tmp_path), started) == str(fresh)


def test_a_build_that_names_only_stale_prgs_announces_nothing(tmp_path):
    started = time.time()
    _prg(tmp_path, "Old.prg", started - 3600)
    assert _announced_prg("reusing Old.prg", str(tmp_path), started) is None


def test_the_newest_wins_when_a_script_names_several_fresh_prgs(tmp_path):
    started = time.time()
    _prg(tmp_path, "First.prg", started + 1)
    last = _prg(tmp_path, "Second.prg", started + 5)
    out = "wrote First.prg\nwrote Second.prg"
    assert _announced_prg(out, str(tmp_path), started) == str(last)


def test_build_refuses_when_only_a_stale_prg_was_named(tmp_path, monkeypatch):
    """End to end: a successful build that names nothing fresh must raise, not
    hand back the stale file it mentioned."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "build.sh").write_text("#!/bin/sh\n")
    _prg(tmp_path, "Old.prg", time.time() - 3600)

    s = SimSession.__new__(SimSession)
    monkeypatch.setattr(session_mod.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(
                            returncode=0, stdout="reusing Old.prg", stderr=""))
    with pytest.raises(SimError) as exc:
        s.build(str(tmp_path), "venu3")
    assert "did not name a fresh one" in str(exc.value)


@pytest.mark.parametrize("given,expected", [
    (None, 2), (0, 0), (1, 1), (3, 3), (10**9, 3), (-5, 0), ("2", 2), ("lots", 2), ([1], 2),
])
def test_step_retries_are_bounded(given, expected):
    """Steps come from the wire (MCP, ciqkit.toml); the REACH chunk's time
    budget assumes a small retry count."""
    step = {} if given is None else {"retries": given}
    assert session_mod._step_retries(step) == expected
