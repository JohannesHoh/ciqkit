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
    # The strict pre-re-send check sees opaque frames here: "identical".
    monkeypatch.setattr(SimSession, "_pixels_changed", lambda self, a, b, ignore=None: False)
    monkeypatch.setattr(SimSession, "SETTLE_POLL_S", 0)
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


# --- a re-send needs a truly identical frame ----------------------------------

_REAL_PIXELS_CHANGED = SimSession._pixels_changed


def _hud(digit_on=False, noise_seed=None):
    """A dark 240x240 HUD; ``digit_on`` lights 6 px of a small digit (the whole
    effect of a BACK that bumps a counter); ``noise_seed`` adds +-2 levels of
    capture noise everywhere."""
    np = pytest.importorskip("numpy")
    Image = pytest.importorskip("PIL.Image")
    arr = np.zeros((240, 240, 3), dtype=np.int16)
    arr[100:113, 60:181] = 90            # static label
    arr[150:161, 120:122] = 200          # the "1"
    if digit_on:
        arr[150:152, 117:120] = 200
    if noise_seed is not None:
        arr += np.random.default_rng(noise_seed).integers(-2, 3, size=arr.shape)
    return Image.fromarray(np.clip(arr, 0, 255).astype("uint8"), "RGB")


def _frame_session(monkeypatch, after, ocr_result=None):
    """act() over real synthetic frames: the first capture is the baseline,
    every later one comes from ``after()``. The coarse test says "no change"
    -- its real verdict on a one-digit change (see test_verify) -- and the
    strict pre-re-send check is the real one."""
    s = _session_with_fake_screen(monkeypatch, changed=False, ocr_result=ocr_result)
    monkeypatch.setattr(SimSession, "_pixels_changed", _REAL_PIXELS_CHANGED)
    monkeypatch.setattr(session_mod.su, "sim_display_box",
                        lambda img, dev: (0, 0, 240, 240))
    frames = {"n": 0}

    def capture(self):
        frames["n"] += 1
        # Frame 1 is the baseline, frame 2 the pre-input stability probe.
        return _hud(noise_seed=frames["n"]) if frames["n"] <= 2 else after()
    monkeypatch.setattr(SimSession, "_capture", capture)
    inputs = []
    monkeypatch.setattr(SimSession, "_do_input",
                        lambda self, *a, **k: inputs.append(a))
    return s, inputs


def test_a_one_digit_change_is_landed_not_resent(monkeypatch):
    """A BACK on a training HUD changed one digit; read as "no effect" it was
    sent three times and the attempts counter went 0 -> 2 -> 5."""
    seeds = iter(range(1, 100))
    s, inputs = _frame_session(
        monkeypatch, lambda: _hud(digit_on=True, noise_seed=next(seeds)))
    monkeypatch.setattr(SimSession, "_resolve_button", lambda self, b: None)
    r = s.act("button", button="esc", delay=0, settle_s=0)
    assert len(inputs) == 1, "a gesture that landed was sent again"
    assert r["ok"] is True and r["confirmed"] is True
    assert r["changed"] is True and r["change"] == "small"


def test_an_identical_frame_is_still_resent(monkeypatch):
    """The simulator does drop inputs; a truly unchanged screen keeps its retries."""
    s, inputs = _frame_session(monkeypatch, lambda: _hud(noise_seed=0))
    r = s.act("tap", delay=0, settle_s=0)
    assert r["ok"] is False and len(inputs) == 3


def test_capture_noise_is_not_mistaken_for_a_landed_gesture(monkeypatch):
    seeds = iter(range(1, 100))
    s, inputs = _frame_session(monkeypatch, lambda: _hud(noise_seed=next(seeds)))
    r = s.act("key", key="enter", delay=0, settle_s=0)
    assert r["ok"] is False and len(inputs) == 3


def test_a_contradicted_anchor_after_a_small_change_fails_without_a_resend(monkeypatch):
    s, inputs = _frame_session(monkeypatch, lambda: _hud(digit_on=True),
                               ocr_result=False)
    r = s.act("tap", anchor=["TOTAL"], delay=0, settle_s=0)
    assert r["ok"] is False and r["change"] == "small"
    assert len(inputs) == 1


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
    monkeypatch.setattr(session_mod._compat, "run_killing_tree",
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


def test_a_late_painting_element_is_not_taken_for_a_landed_gesture(monkeypatch):
    """The baseline was stabilised with the coarse test only, so a small icon
    that paints after it must not read as the input's effect: a dropped input
    would be called landed and never re-sent."""
    ImageDraw = pytest.importorskip("PIL.ImageDraw")

    def with_icon(img):
        ImageDraw.Draw(img).rectangle([200, 20, 203, 23], fill=(255, 255, 255))
        return img

    s, inputs = _frame_session(monkeypatch, lambda: with_icon(_hud(noise_seed=9)))
    frames = {"n": 0}

    def capture(self):
        frames["n"] += 1
        base = _hud(noise_seed=frames["n"])
        # baseline: no icon; the probe (frame 2) already sees it appear
        return base if frames["n"] == 1 else with_icon(base)
    monkeypatch.setattr(SimSession, "_capture", capture)
    r = s.act("tap", delay=0, settle_s=0)
    assert r["ok"] is False and len(inputs) == 3


def test_no_display_box_means_no_strict_proof(monkeypatch):
    """On a scaled (Retina) capture sim_display_box is None; comparing the
    whole window would count title-bar or banner pixels as the gesture."""
    s = _session_with_fake_screen(monkeypatch, changed=False, ocr_result=None)
    monkeypatch.setattr(SimSession, "_pixels_changed", _REAL_PIXELS_CHANGED)
    monkeypatch.setattr(session_mod.su, "sim_display_box", lambda img, dev: None)
    monkeypatch.setattr(SimSession, "_face_box", lambda self, img: None)
    assert s._pixels_changed(_hud(), _hud(digit_on=True)) is False
