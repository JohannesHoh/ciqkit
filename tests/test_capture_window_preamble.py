"""capture_window's preamble: what runs before the pixels are read.

The window bookkeeping in front of every macOS grab -- AppleScript position
probe, restore, focus, settle -- costs ~1.85 s and a one-shot screenshot wants
all of it. A sampling loop that already raised the window wants none of it, and
skipping it is what took `record` from ~0.5 fps to ~4.6. Every other test in
the suite stubs `SimSession._capture`, so they pin record()'s CALL pattern and
nothing at all pins the callee: inverting the `if refocus:` branch, breaking
the `grab_settle_s=None -> DEFAULT_GRAB_SETTLE_S` resolution, or making the
fast path skip the grab itself would all pass. These pin the contract.

Runs without a simulator and without a display: the whole macOS branch is
stubbed down to the `screencapture` subprocess, which writes a synthetic PNG.
"""
from types import SimpleNamespace

import pytest

pytest.importorskip("numpy")
pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from ciqkit.capture import screenshot_utils as su  # noqa: E402


class _Calls(list):
    """A call log that can also carry the fixture's two control knobs."""


class _FakeWindow:
    """Stand-in for the module's `_MacWindow`, which only exists when the
    module is imported on macOS. Mirrors the handle contract capture_window
    relies on: the rect, the capture-derived geometry side-channel, and
    `rect()` for the geometry cache."""

    def __init__(self):
        self.left, self.top, self.width, self.height = 100, 200, 300, 400
        self.content_left = self.content_top = 0
        self.content_w = self.content_h = 0
        self.retina_scale = 1.0
        self.geometry_captured = False

    def rect(self):
        return (self.left, self.top, self.width, self.height)


@pytest.fixture
def mac(monkeypatch, tmp_path):
    """The macOS capture path with every side effect recorded, not performed.

    `time` and `subprocess` are replaced by rebinding the NAMES in this module's
    namespace, never by mutating the real modules: a `time.sleep` patched
    process-wide is inherited by every live background thread, and the broker
    tests keep some, so mutating it turns their waits into busy loops and fails
    an unrelated test file later in the run.
    """
    calls = _Calls()
    regions = []
    monkeypatch.setattr(su, "IS_WIN", False)
    monkeypatch.setattr(su, "IS_MAC", True)
    probe_result = {"window": None}

    def fake_probe():
        calls.append("probe")
        return probe_result["window"]

    monkeypatch.setattr(su, "find_sim_window", fake_probe)
    monkeypatch.setattr(su, "restore_window",
                        lambda _h: calls.append("restore"))
    monkeypatch.setattr(su, "focus_window", lambda _h: calls.append("focus"))
    monkeypatch.setattr(
        su, "time", SimpleNamespace(sleep=lambda s: calls.append(("sleep", s))))

    def fake_screencapture(argv, **_kw):
        assert argv[0] == "screencapture"
        calls.append("grab")
        regions.append(argv[argv.index("-R") + 1])
        # Lit content inside a black surround: that is what the chrome trim
        # looks for (it keeps rows/cols brighter than 5), so the trimmed box is
        # exactly the pasted square and the offsets are predictable.
        img = Image.new("RGB", (600, 800), (0, 0, 0))
        img.paste(Image.new("RGB", (400, 400), (200, 200, 200)), (100, 200))
        img.save(argv[-1])
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(su, "subprocess",
                        SimpleNamespace(run=fake_screencapture))
    # Quartz is tried first and "fails" here, so the CLI path above is what
    # these tests exercise; test_quartz_grab_skips_the_cli covers the other.
    monkeypatch.setattr(su, "_quartz_region_png", lambda *_a: False)
    calls.probe_result = probe_result
    calls.regions = regions
    return calls


def _moved_window(dx, dy):
    w = _FakeWindow()
    w.left += dx
    w.top += dy
    return w


def _sleeps(calls):
    return [c[1] for c in calls if isinstance(c, tuple) and c[0] == "sleep"]


def test_the_default_call_still_pays_the_full_preamble(mac):
    """shot()'s path is unchanged: probe, restore, focus, settle, THEN grab.

    Asserted as one ordered sequence, not as a set of steps plus a separate
    list of sleeps: the settle's whole job is to sit between the raise and the
    grab, so a refactor that moved it after the grab would satisfy every
    per-step assertion while making the knob meaningless.
    """
    img, scale = su.capture_window(_FakeWindow())
    assert list(mac) == ["probe", "restore", "focus",
                         ("sleep", su.DEFAULT_GRAB_SETTLE_S), "grab"]
    assert img.size[0] > 50 and scale == 1.0


def test_the_fast_path_skips_the_preamble_but_not_the_grab(mac):
    """The point of the whole change -- and 'skips the grab too' would be a
    silently passing disaster, so assert the grab explicitly."""
    img, _ = su.capture_window(_FakeWindow(), refocus=False, grab_settle_s=0)
    assert [c for c in mac if isinstance(c, str)] == ["grab"]
    assert _sleeps(mac) == []
    assert img.size[0] > 50


def test_refocus_and_settle_are_independent(mac):
    """A refocus frame raises the window and takes a SHORT settle; record()
    relies on being able to ask for exactly that."""
    su.capture_window(_FakeWindow(), refocus=True, grab_settle_s=0.2)
    assert list(mac) == ["probe", "restore", "focus", ("sleep", 0.2), "grab"]

    mac.clear()
    su.capture_window(_FakeWindow(), refocus=False, grab_settle_s=0.5)
    assert list(mac) == [("sleep", 0.5), "grab"]


def test_none_means_the_module_default_and_zero_means_zero(mac):
    """`grab_settle_s=0` must not be read as falsy-so-use-the-default; that
    mistake would silently restore the 1 s per frame."""
    su.capture_window(_FakeWindow(), grab_settle_s=None)
    assert _sleeps(mac) == [su.DEFAULT_GRAB_SETTLE_S]
    mac.clear()
    su.capture_window(_FakeWindow(), grab_settle_s=0)
    assert _sleeps(mac) == []


def test_the_fast_path_still_publishes_the_click_geometry(mac):
    """pm_click/pm_swipe convert screen coords using retina_scale and the
    content offsets that capture_window writes onto the handle. Those are NOT
    part of the preamble and must survive the fast path, or gestures after a
    clip would land in the wrong place."""
    hwnd = _FakeWindow()
    su.capture_window(hwnd, refocus=False, grab_settle_s=0)
    assert hwnd.geometry_captured is True
    assert hwnd.retina_scale == pytest.approx(600 / 300)
    assert (hwnd.content_left, hwnd.content_top) == (100, 200)
    assert (hwnd.content_w, hwnd.content_h) == (400, 400)


def test_the_probe_result_is_actually_applied_to_the_grab_region(mac):
    """The probe exists to catch a window that MOVED. Reading its answer and
    discarding it would look identical in every step-order assertion, and every
    later grab would photograph the old screen region."""
    mac.probe_result["window"] = _moved_window(dx=40, dy=25)
    su.capture_window(_FakeWindow())
    assert mac.regions == ["140,225,300,400"]


def test_the_fast_path_does_not_re_read_the_window_position(mac):
    """...and skipping the probe means keeping the caller's rect, not silently
    falling back to a stale module-level one."""
    mac.probe_result["window"] = _moved_window(dx=40, dy=25)
    su.capture_window(_FakeWindow(), refocus=False, grab_settle_s=0)
    assert "probe" not in mac
    assert mac.regions == ["100,200,300,400"]


def test_the_capture_geometry_is_published_to_the_module_cache(mac):
    """pm_click/pm_swipe do not use the handle capture_window wrote to --
    _rebind() throws it away and builds a fresh one. The offsets only survive
    because capture_window also stores them module-side, keyed on the rect, so
    dropping _remember_geometry() would silently shift every tap after a clip
    while leaving the on-handle assertions above perfectly green."""
    su.capture_window(_FakeWindow(), refocus=False, grab_settle_s=0)
    fresh = _FakeWindow()                      # what _rebind() would hand back
    assert fresh.geometry_captured is False
    su._restore_geometry(fresh)
    assert fresh.geometry_captured is True
    assert (fresh.content_left, fresh.content_top) == (100, 200)
    assert fresh.retina_scale == pytest.approx(600 / 300)


def test_quartz_grab_skips_the_cli(mac, monkeypatch):
    """The `screencapture` CLI can hang machine-wide while Quartz answers in
    milliseconds (2026-09-28); a Quartz grab that succeeds must not also run
    the CLI."""
    def quartz(x, y, w, h, path):
        mac.append("quartz")
        img = Image.new("RGB", (600, 800), (0, 0, 0))
        img.paste(Image.new("RGB", (400, 400), (200, 200, 200)), (100, 200))
        img.save(path)
        return True

    monkeypatch.setattr(su, "_quartz_region_png", quartz)
    img, _ = su.capture_window(_FakeWindow(), refocus=False, grab_settle_s=0)
    assert "quartz" in mac and "grab" not in mac
    assert img.size == (400, 400)
