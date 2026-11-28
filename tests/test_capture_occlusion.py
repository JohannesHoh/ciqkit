"""Window-content grab, occlusion detection and memory pressure (macOS).

2026-11-19: macOS's "out of application memory" panel sat over the simulator and
every region grab contained it. These pin: the window-id grab is preferred, the
region grab stays as the fallback, covers are named, a fallback grab under a
cover is refused, and memory pressure is read once per job without ever raising.
"""
import subprocess
from types import SimpleNamespace

import pytest

pytest.importorskip("numpy")
pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from ciqkit.capture import screenshot_utils as su  # noqa: E402
from ciqkit.capture import simwindows as sw  # noqa: E402
from ciqkit.sim import preflight  # noqa: E402

RECT = (100, 200, 300, 400)
_REAL_PRESSURE = preflight.memory_pressure   # conftest stubs the module attr per test


def _info(owner, x, y, w, h, *, wid=1, name="", layer=0, alpha=1.0):
    return {"kCGWindowNumber": wid, "kCGWindowOwnerName": owner,
            "kCGWindowName": name, "kCGWindowLayer": layer, "kCGWindowAlpha": alpha,
            "kCGWindowBounds": {"X": x, "Y": y, "Width": w, "Height": h}}


# ── pure helpers ────────────────────────────────────────────────────────────
def test_pick_window_id_matches_owner_and_bounds():
    infos = [_info("Finder", *RECT, wid=5),
             _info("simulator", 0, 0, 50, 50, wid=6),
             _info("simulator", *RECT, wid=7, name="CIQ Simulator - venu3 (9.1)")]
    assert sw.pick_window_id(infos, RECT) == 7
    assert sw.pick_window_id(infos, (900, 900, 300, 400)) is None
    assert sw.pick_window_id([], RECT) is None


def test_pick_window_id_ignores_non_normal_layers_and_prefers_titled():
    infos = [_info("simulator", *RECT, wid=8, layer=25),
             _info("simulator", *RECT, wid=9, name="Webpage Window"),
             _info("simulator", 101, 200, 300, 400, wid=10, name="CIQ Simulator - x")]
    assert sw.pick_window_id(infos, RECT) == 10


def test_occluders_intersecting_vs_not_vs_tiny_vs_invisible():
    infos = [
        _info("CoreServicesUIAgent", 150, 250, 200, 200, name="Out of memory"),  # covers
        _info("Safari", 1000, 1000, 400, 400),                                   # elsewhere
        _info("Cursor", 150, 250, 8, 8),                                         # tiny
        _info("Ghost", 100, 200, 300, 400, alpha=0.0),                           # invisible
        _info("Empty", 100, 200, 0, 0),                                          # zero size
    ]
    occ = sw.find_occluders(infos, RECT)
    assert [o["owner"] for o in occ] == ["CoreServicesUIAgent"]
    assert occ[0]["title"] == "Out of memory"
    assert "CoreServicesUIAgent: Out of memory" in sw.describe_occluders(occ)


def test_display_sized_overlay_hosts_are_not_covers():
    infos = [_info("Dock", 0, 0, 1920, 1080, layer=20, name="Dock"),
             _info("Notification Centre", 0, 0, 1920, 1080, layer=21),
             _info("Notification Centre", 140, 220, 300, 80, layer=23, name="banner")]
    occ = sw.find_occluders(infos, RECT, displays=[(1920, 1080)])
    assert [o["title"] for o in occ] == ["banner"]
    # without display sizes the hosts cannot be told apart (documented limit)
    assert len(sw.find_occluders(infos, RECT)) == 3


def test_occluders_biggest_first_and_edge_touch_ignored():
    infos = [_info("Small", 100, 200, 60, 60), _info("Big", 100, 200, 250, 250),
             _info("Adjacent", 400, 200, 100, 400)]   # touches the right edge only
    assert [o["owner"] for o in sw.find_occluders(infos, RECT)] == ["Big", "Small"]


# ── capture_window ──────────────────────────────────────────────────────────
class _Win:
    def __init__(self):
        self.left, self.top, self.width, self.height = RECT
        self.content_left = self.content_top = self.content_w = self.content_h = 0
        self.retina_scale = 1.0
        self.geometry_captured = False

    def rect(self):
        return RECT


def _png(path):
    img = Image.new("RGB", (600, 800), (0, 0, 0))
    img.paste(Image.new("RGB", (400, 400), (200, 200, 200)), (100, 200))
    img.save(path)


@pytest.fixture
def grab(monkeypatch):
    log = []
    monkeypatch.setattr(su, "IS_WIN", False)
    monkeypatch.setattr(su, "IS_MAC", True)
    monkeypatch.setattr(su, "_park_pointer", lambda _w: None)
    monkeypatch.setattr(su, "_OCCLUDED", [])
    state = {"wid": 42, "occ": [], "window_ok": True, "region_ok": True}
    monkeypatch.setattr(su, "_mac_sim_window_id", lambda _r: (state["wid"], state["occ"]))

    def window_png(wid, x, y, w, h, path):
        log.append(("window", wid, (x, y, w, h)))
        if state["window_ok"]:
            _png(path)
        return state["window_ok"]

    def region_png(x, y, w, h, path):
        log.append("region")
        if state["region_ok"]:
            _png(path)
        return state["region_ok"]

    def cli(argv, **_k):
        log.append("cli")
        _png(argv[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(su, "_quartz_window_png", window_png)
    monkeypatch.setattr(su, "_quartz_region_png", region_png)
    monkeypatch.setattr(su, "subprocess", SimpleNamespace(run=cli))
    state["log"] = log
    return state


def test_window_id_grab_is_chosen_and_geometry_is_unchanged(grab):
    w = _Win()
    img, _ = su.capture_window(w, refocus=False, grab_settle_s=0)
    assert grab["log"] == [("window", 42, RECT)]       # same rect as the region grab
    assert w.grab_mode == "window" and w.occluded_by == []
    assert img.size == (400, 400) and w.retina_scale == pytest.approx(2.0)
    assert (w.content_left, w.content_top) == (100, 200)


def test_window_grab_succeeds_under_a_cover_and_reports_it(grab):
    grab["occ"] = [{"owner": "UserNotificationCenter", "title": "Memory", "area": 9999}]
    w = _Win()
    su.capture_window(w, refocus=False, grab_settle_s=0)
    assert w.grab_mode == "window"
    assert w.occluded_by == grab["occ"]
    assert su.drain_occluders()[0]["owner"] == "UserNotificationCenter"
    assert su.drain_occluders() == []


@pytest.mark.parametrize("why", ["no_id", "empty_window_grab"])
def test_falls_back_to_the_region_grab(grab, why):
    if why == "no_id":
        grab["wid"] = None
    else:
        grab["window_ok"] = False
    w = _Win()
    su.capture_window(w, refocus=False, grab_settle_s=0)
    assert w.grab_mode == "region" and "region" in grab["log"]


def test_falls_back_to_the_cli_when_quartz_is_unavailable(grab):
    grab["wid"] = None
    grab["region_ok"] = False
    w = _Win()
    su.capture_window(w, refocus=False, grab_settle_s=0)
    assert w.grab_mode == "screencapture" and grab["log"][-1] == "cli"


def test_fallback_grab_under_a_cover_fails_naming_it(grab):
    grab["window_ok"] = False
    grab["occ"] = [{"owner": "UserNotificationCenter",
                    "title": "Your system has run out of application memory", "area": 1}]
    with pytest.raises(su.OccludedError) as e:
        su.capture_window(_Win(), refocus=False, grab_settle_s=0)
    assert str(e.value) == (
        "simulator window is covered by UserNotificationCenter: Your system has run "
        "out of application memory -- the shot would contain it")
    assert "region" not in grab["log"] and "cli" not in grab["log"]


def test_session_turns_the_cover_into_a_sim_error(grab, monkeypatch):
    from ciqkit.sim.errors import SimError
    from ciqkit.sim.session import SimSession
    grab["window_ok"] = False
    grab["occ"] = [{"owner": "X", "title": "Y", "area": 1}]
    s = SimSession.__new__(SimSession)
    s._hwnd = _Win()
    with pytest.raises(SimError, match="covered by X: Y"):
        s._capture(grab_settle_s=0, refocus=False)


def test_with_dismissed_attaches_occluded_by_and_a_warning(monkeypatch):
    from ciqkit.sim import session
    monkeypatch.setattr(su, "_OCCLUDED", [])

    @session._with_dismissed
    def fn(self):
        su.note_occluded([{"owner": "Dialog", "title": "Memory", "area": 5}])
        return {"ok": True}
    r = fn(None)
    assert r["occluded_by"] == [{"owner": "Dialog", "title": "Memory", "area": 5}]
    assert "Dialog: Memory" in r["warnings"][0]


# ── memory pressure ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("out,level", [("1\n", 1), ("2\n", 2), ("4\n", 4),
                                       ("3\n", None), ("", None), ("x", None)])
def test_memory_pressure_levels(monkeypatch, out, level):
    monkeypatch.setattr(preflight.sys, "platform", "darwin")
    monkeypatch.setattr(preflight.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=out, stderr=""))
    assert _REAL_PRESSURE() == level


def test_memory_pressure_never_raises(monkeypatch):
    monkeypatch.setattr(preflight.sys, "platform", "darwin")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("sysctl", 2)
    monkeypatch.setattr(preflight.subprocess, "run", boom)
    assert _REAL_PRESSURE() is None
    monkeypatch.setattr(preflight.sys, "platform", "linux")
    assert _REAL_PRESSURE() is None


def test_pressure_verdicts():
    assert preflight.pressure_verdict(1) is None
    assert preflight.pressure_verdict(None) is None
    assert preflight.pressure_verdict(2)[0] == "warn"
    kind, msg = preflight.pressure_verdict(4)
    assert kind == "fail" and "out of application memory" in msg


# ── broker: once per job at start ───────────────────────────────────────────
def _broker_job(monkeypatch, level):
    import threading
    import time

    from ciqkit.sim import broker as bm
    from ciqkit.sim.protocol import Chunk, ChunkKind, Job, JobState, Op
    from test_scheduler import FakeSession  # noqa: F401
    calls = []
    monkeypatch.setattr(preflight, "memory_pressure", lambda: calls.append(1) or level)
    b = bm.Broker(session=FakeSession())
    threading.Thread(target=b.scheduler, daemon=True).start()
    job = Job(app="a", label="l", chunks=[Chunk(ChunkKind.ACT, {}, expected_s=0.1, max_s=5),
                                          Chunk(ChunkKind.ACT, {}, expected_s=0.1, max_s=5)])
    b.handle({"op": Op.SUBMIT, "job": job.to_dict()})
    end = time.time() + 15
    while time.time() < end and b.jobs[job.id].state not in (JobState.DONE, JobState.FAILED):
        time.sleep(0.02)
    b.handle({"op": Op.SHUTDOWN})
    return b.jobs[job.id], calls


def test_broker_fails_a_job_fast_on_critical_pressure(monkeypatch):
    j, calls = _broker_job(monkeypatch, 4)
    assert j.state == "failed" and "out of application memory" in j.error
    assert j.results == []


def test_broker_warns_once_on_elevated_pressure(monkeypatch):
    j, calls = _broker_job(monkeypatch, 2)
    assert j.state == "done" and len(j.warnings) == 1 and "elevated" in j.warnings[0]
    assert len(calls) == 1          # once per job, not per chunk


def test_broker_normal_or_unreadable_pressure_is_silent(monkeypatch):
    for level in (1, None):
        j, _ = _broker_job(monkeypatch, level)
        assert j.state == "done" and j.warnings == []
