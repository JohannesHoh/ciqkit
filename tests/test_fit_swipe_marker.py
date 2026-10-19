"""FIT playback on load, explicit/edge swipes, console-marker shots.

All fixtures: AppleScript output and the app console are fed in as text/files,
and the session's GUI calls are replaced, so no simulator, GUI or macOS is needed.
"""
import os

import pytest

from ciqkit.capture import screenshot_utils as su
from ciqkit.capture import simwindows as sw
from ciqkit.sim import console
from ciqkit.sim import jobs
from ciqkit.sim import protocol as proto
from ciqkit.sim import session as session_mod
from ciqkit.sim import broker as broker_mod
from ciqkit.sim.broker import Broker
from ciqkit.sim.protocol import Chunk, ChunkKind, Job
from ciqkit.sim.session import SimError, SimSession

broker_mod.leaselib = None


def _session(tmp_path):
    s = SimSession.__new__(SimSession)
    s.out_dir = str(tmp_path)
    s._console_path = str(tmp_path / "console.log")
    s._console_pos = 0
    s._hwnd, s._canvas, s.current = object(), object(), ("app", "venu3")
    return s


# ── 1. FIT playback ──────────────────────────────────────────────────────────
STATE = "SRC=FIT/GPX Playable File\nTXT=Data Source\nTXT=/r/a.fit\nTXT=Load File\nTXT=00:00:07 / 00:00:30\nTXT=\n"


def test_parse_fit_state_reads_source_file_and_clock():
    st = sw.parse_fit_state(STATE)
    assert st == {"source": "FIT/GPX Playable File", "file": "/r/a.fit",
                  "elapsed_s": 7, "total_s": 30}


def test_parse_fit_state_before_play_and_errors():
    st = sw.parse_fit_state("SRC=x\nTXT=/r/a.GPX\nTXT=00:00:00 / 01:02:03\n")
    assert st["file"] == "/r/a.GPX" and st["elapsed_s"] == 0 and st["total_s"] == 3723
    assert sw.parse_fit_state("ERR no Activity Data window") == {
        "error": "no Activity Data window"}
    assert sw.parse_fit_state("")["file"] is None


def test_start_fit_playback_refuses_off_mac_and_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(su, "IS_MAC", False)
    assert "macOS only" in su.start_fit_playback(str(tmp_path / "a.fit"))["reason"]
    monkeypatch.setattr(su, "IS_MAC", True)
    assert "no such file" in su.start_fit_playback(str(tmp_path / "a.fit"))["reason"]


class _Proc:
    def __init__(self, out="OK", rc=0):
        self.stdout, self.stderr, self.returncode = out, "", rc


def _drive(monkeypatch, tmp_path, states, load="OK"):
    """Run start_fit_playback against scripted AppleScript answers."""
    f = tmp_path / "a.fit"
    f.write_bytes(b"x")
    monkeypatch.setattr(su, "IS_MAC", True)
    monkeypatch.setattr(su.time, "sleep", lambda s: None)
    monkeypatch.setattr(su.subprocess, "run", lambda *a, **k: _Proc(load))
    seq = iter(states)
    calls = []

    def fake(script, *args, **kw):
        calls.append(args[0])
        return sw.parse_fit_state(next(seq))
    monkeypatch.setattr(su, "_fit_osascript", fake)
    return str(f), calls


def _state(path, elapsed):
    return f"SRC=s\nTXT={path}\nTXT=00:00:{elapsed:02d} / 00:00:30\n"


def test_fit_playback_verified_when_clock_advances(monkeypatch, tmp_path):
    p = str(tmp_path / "a.fit")
    path, calls = _drive(monkeypatch, tmp_path,
                         [_state(p, 0), _state(p, 0), _state(p, 0), _state(p, 2), "OK"])
    r = su.start_fit_playback(path)
    assert r["ok"] and r["elapsed_s"] == 2 and r["total_s"] == 30
    assert calls == ["state", "play", "state", "state", "close"]


def test_fit_playback_fails_when_clock_never_advances(monkeypatch, tmp_path):
    p = str(tmp_path / "a.fit")
    clock = iter([0.0, 0.0, 5.0, 50.0, 99.0, 99.0])
    monkeypatch.setattr(su.time, "monotonic", lambda: next(clock))
    path, _ = _drive(monkeypatch, tmp_path, [_state(p, 0)] * 10)
    r = su.start_fit_playback(path, advance_s=12)
    assert not r["ok"] and "did not advance" in r["reason"]


def test_fit_playback_fails_when_the_panel_took_another_file(monkeypatch, tmp_path):
    path, _ = _drive(monkeypatch, tmp_path, [_state("/other/b.fit", 0)])
    r = su.start_fit_playback(path)
    assert not r["ok"] and "not the requested file" in r["reason"]


def test_fit_playback_reports_a_failed_load_script(monkeypatch, tmp_path):
    path, _ = _drive(monkeypatch, tmp_path, [], load="ERR no Activity Data window")
    assert "no Activity Data window" in su.start_fit_playback(path)["reason"]


def test_load_with_fit_starts_playback_after_the_load(monkeypatch):
    s = SimSession.__new__(SimSession)
    order = []
    monkeypatch.setattr(SimSession, "_load", lambda self, *a: order.append("load") or {"ok": True})
    monkeypatch.setattr(su, "start_fit_playback",
                        lambda p: order.append(("fit", p)) or {"ok": True, "file": p})
    r = s.load("app", "/x.prg", fit="/r/a.fit")
    assert order == ["load", ("fit", "/r/a.fit")] and r["fit"]["ok"]
    order.clear()
    assert "fit" not in s.load("app", "/x.prg")
    assert order == ["load"]


def test_load_fails_when_playback_did_not_start(monkeypatch):
    s = SimSession.__new__(SimSession)
    monkeypatch.setattr(SimSession, "_load", lambda self, *a: {"ok": True})
    monkeypatch.setattr(su, "start_fit_playback",
                        lambda p: {"ok": False, "reason": "clock stuck"})
    with pytest.raises(SimError, match="FIT playback did not start: clock stuck"):
        s.load("app", "/x.prg", fit="/r/a.fit")


def test_act_fit_returns_a_failure_instead_of_raising(monkeypatch):
    s = SimSession.__new__(SimSession)
    monkeypatch.setattr(su, "start_fit_playback", lambda p: {"ok": False, "reason": "no"})
    r = s.act("fit", path="/r/a.fit")
    assert r["ok"] is False and "did not start" in r["reason"]


def test_load_chunk_carries_the_fit_file_and_the_broker_forwards_it(tmp_path):
    c = jobs.load_chunk(device="venu3", project=str(tmp_path), prg="bin/a.prg",
                        fit="~/r/a.fit")
    assert c.args["fit"] == os.path.abspath(os.path.expanduser("~/r/a.fit"))
    assert os.path.isabs(c.args["fit"]) and "~" not in c.args["fit"]
    assert "fit" not in jobs.load_chunk(device="venu3", project=str(tmp_path)).args

    seen = {}

    class S:
        def load(self, app, prg, device, **kw):
            seen.update(kw)
            return {"ok": True}
    b = Broker()
    b.session = S()
    b._dispatch(Job(app="a", chunks=[]), c)
    assert seen["fit"] == c.args["fit"]


def test_validate_job_rejects_a_blank_fit(tmp_path):
    bad = Chunk(ChunkKind.LOAD, {"prg": "/x.prg", "fit": ""})
    assert any("fit must be" in e for e in proto.validate_job(Job(app="a", chunks=[bad])))
    step = Chunk(ChunkKind.REACH, {"steps": [{"kind": "fit"}]})
    assert any("needs a path" in e for e in proto.validate_job(Job(app="a", chunks=[step])))


# ── 2. edge swipes ───────────────────────────────────────────────────────────
def test_default_swipe_has_no_explicit_geometry():
    assert proto.swipe_spec({"kind": "swipe", "direction": "left"}) is None
    assert proto.swipe_spec({"kind": "swipe"}) is None


def test_explicit_from_and_dist():
    spec = proto.swipe_spec({"kind": "swipe", "direction": "left",
                             "from": [0.97, 0.5], "dist": 0.4})
    assert spec == {"from": (0.97, 0.5), "direction": "left", "dist": 0.4}
    # from alone: default length; dist alone: starts at the centre
    assert proto.swipe_spec({"from": [0.1, 0.2], "direction": "down"})["dist"] == proto.EDGE_DIST
    assert proto.swipe_spec({"dist": 0.3, "direction": "up"})["from"] == (0.5, 0.5)


@pytest.mark.parametrize("edge,frm,direction", [
    ("right", (0.97, 0.5), "left"), ("left", (0.03, 0.5), "right"),
    ("top", (0.5, 0.03), "down"), ("bottom", (0.5, 0.97), "up")])
def test_edge_swipe_starts_at_the_edge_and_pulls_inward(edge, frm, direction):
    assert proto.swipe_spec({"kind": "swipe", "edge": edge}) == {
        "from": frm, "direction": direction, "dist": proto.EDGE_DIST}
    # an explicit dist/direction still wins
    spec = proto.swipe_spec({"edge": edge, "dist": 0.6, "direction": "up"})
    assert spec["dist"] == 0.6 and spec["direction"] == "up"


@pytest.mark.parametrize("step,frag", [
    ({"kind": "swipe", "from": [0.5]}, "[x, y]"),
    ({"kind": "swipe", "from": [1.5, 0.5]}, "[x, y]"),
    ({"kind": "swipe", "from": "left"}, "[x, y]"),
    ({"kind": "swipe", "dist": 0}, "dist"),
    ({"kind": "swipe", "dist": "far"}, "dist"),
    ({"kind": "swipe", "edge": "middle"}, "edge"),
    ({"kind": "swipe", "direction": "sideways"}, "direction")])
def test_bad_swipe_steps_are_refused(step, frag):
    assert any(frag in e for e in proto.step_errors(step))


def test_good_swipe_steps_validate():
    for step in ({"kind": "swipe", "direction": "left", "from": [0.97, 0.5], "dist": 0.4},
                 {"kind": "swipe", "edge": "right"}, {"kind": "swipe"}):
        assert proto.step_errors(step) == []


def test_drag_ends():
    assert su.drag_ends(100, 50, "left", 40) == (60, 50)
    assert su.drag_ends(100, 50, "down", 40) == (100, 90)
    with pytest.raises(ValueError):
        su.drag_ends(0, 0, "diag", 1)


def test_pm_swipe_from_frac_starts_where_told_and_runs_dist(monkeypatch):
    drags = []
    monkeypatch.setattr(su, "_prepare_input", lambda c: None)
    monkeypatch.setattr(su, "content_point", lambda c, fx, fy: (fx * 200, fy * 200))
    monkeypatch.setattr(su, "content_span", lambda c, f: int(200 * f))
    monkeypatch.setattr(su, "_drag", lambda c, *a: drags.append(a))
    su.pm_swipe_from_frac(object(), 0.97, 0.5, direction="left", dist=0.4, delay=0)
    sx, sy, ex, ey, delay = drags[0]
    assert (sx, sy) == (194.0, 100.0) and (ex, ey) == (194.0 - 80, 100.0)


def test_session_swipe_uses_the_explicit_geometry_only_when_given(monkeypatch):
    s = SimSession.__new__(SimSession)
    s._hwnd, s._canvas = object(), object()
    got = []
    monkeypatch.setattr(SimSession, "_rebind", lambda self: None)
    monkeypatch.setattr(su, "restore_window", lambda h: None)
    monkeypatch.setattr(session_mod.time, "sleep", lambda x: None)
    monkeypatch.setattr(su, "pm_swipe_frac", lambda c, **k: got.append(("default", k)))
    monkeypatch.setattr(su, "pm_swipe_from_frac",
                        lambda c, fx, fy, **k: got.append(("from", (fx, fy), k)))
    s._do_input("swipe", 0.5, 0.5, "left", "enter", 0)
    spec = proto.swipe_spec({"edge": "right"})
    s._do_input("swipe", 0.5, 0.5, "left", "enter", 0, swipe=spec)
    assert got[0][0] == "default" and got[0][1]["direction"] == "left"
    assert got[1] == ("from", (0.97, 0.5), {"direction": "left", "dist": 0.4, "delay": 0})


def test_reach_and_broker_pass_the_swipe_spec(monkeypatch):
    s = SimSession.__new__(SimSession)
    seen = []
    monkeypatch.setattr(SimSession, "act", lambda self, kind, *a, **k: seen.append(k.get("swipe"))
                        or {"ok": True})
    monkeypatch.setattr(SimSession, "_capture", lambda self: None)
    s.reach([{"kind": "swipe", "direction": "left", "from": [0.97, 0.5], "dist": 0.4},
             {"kind": "swipe", "direction": "up"}, {"kind": "tap"}])
    assert seen[0] == {"from": (0.97, 0.5), "direction": "left", "dist": 0.4}
    assert seen[1] is None and seen[2] is None

    calls = []

    class Rec:
        def act(self, kind, **kw):
            calls.append((kind, kw))
            return {"ok": True}
    b = Broker(session=Rec())
    b._dispatch(Job(app="a", chunks=[]),
                Chunk(ChunkKind.ACT, {"kind": "swipe", "edge": "right"}))
    b._dispatch(Job(app="a", chunks=[]),
                Chunk(ChunkKind.ACT, {"kind": "swipe", "direction": "left"}))
    assert calls[0][1]["swipe"]["from"] == (0.97, 0.5)
    assert "swipe" not in calls[1][1] and calls[1][1]["direction"] == "left"


# ── 3. console markers ───────────────────────────────────────────────────────
LOG = b"boot\nMX_SHOT page1 ready\nnoise\nMX_SHOT page2\nMX_SHOT page2\nhalf MX_SH"


def test_find_marker_returns_the_line_and_the_next_offset():
    line, end = console.find_marker(LOG, 0, "MX_SHOT page1")
    assert line == "MX_SHOT page1 ready"
    line2, end2 = console.find_marker(LOG, end, "MX_SHOT page2")
    assert line2 == "MX_SHOT page2" and end2 > end
    # the same text printed twice is two hits, in order
    assert console.find_marker(LOG, end2, "MX_SHOT page2")[0] == "MX_SHOT page2"


def test_find_marker_ignores_a_half_written_line_and_supports_regex():
    assert console.find_marker(LOG, 0, "half MX_SH") is None
    assert console.find_marker(b"x\nCAPSTEP 3 menu\n", 0, r"CAPSTEP \d+ menu",
                               regex=True)[0] == "CAPSTEP 3 menu"
    assert console.find_marker(b"CAPSTEP x\n", 0, r"CAPSTEP \d", regex=True) is None


def test_wait_marker_reads_on_from_the_cursor(tmp_path):
    s = _session(tmp_path)
    (tmp_path / "console.log").write_bytes(LOG)
    a = s.wait_marker("MX_SHOT page2", 1)
    assert a["line"] == "MX_SHOT page2"
    # page1 was printed BEFORE the cursor: it is not found any more...
    with pytest.raises(SimError, match="did not appear"):
        s.wait_marker("MX_SHOT page1", 0.2)
    # ...and the second print of page2 is still there
    assert s.wait_marker("MX_SHOT page2", 1)["line"] == "MX_SHOT page2"


def test_wait_marker_sees_a_line_that_arrives_while_waiting(tmp_path):
    s = _session(tmp_path)
    log = tmp_path / "console.log"
    log.write_bytes(b"boot\n")
    real_sleep = session_mod.time.sleep
    n = {"i": 0}

    def sleep(x):
        n["i"] += 1
        if n["i"] == 3:
            log.write_bytes(b"boot\nMX_SHOT late\n")
        real_sleep(0)
    import unittest.mock as m
    with m.patch.object(session_mod.time, "sleep", sleep):
        assert s.wait_marker("MX_SHOT late", 5)["line"] == "MX_SHOT late"


def test_wait_marker_timeout_shows_the_console_tail(tmp_path):
    s = _session(tmp_path)
    (tmp_path / "console.log").write_bytes(b"one\ntwo\n")
    with pytest.raises(SimError) as e:
        s.wait_marker("NOPE", 0.1)
    assert "two" in str(e.value) and "pass force" in str(e.value)
    s2 = _session(tmp_path / "empty")
    s2._console_path = str(tmp_path / "missing.log")
    with pytest.raises(SimError, match="printed nothing"):
        s2.wait_marker("NOPE", 0.1)


def test_shot_waits_for_the_marker_before_capturing(monkeypatch, tmp_path):
    from PIL import Image
    s = _session(tmp_path)
    (tmp_path / "console.log").write_bytes(b"MX_SHOT go\n")
    order = []
    monkeypatch.setattr(SimSession, "_capture", lambda self, **k: order.append("capture")
                        or Image.new("RGB", (10, 10)))
    monkeypatch.setattr(SimSession, "_rebind", lambda self: None)
    monkeypatch.setattr(SimSession, "_framed", lambda self, img: None)
    monkeypatch.setattr(SimSession, "_crop_face", lambda self, img: None)
    monkeypatch.setattr(SimSession, "_storage_crash_check", lambda self, od: None)
    monkeypatch.setattr(su, "dismiss_modal_dialogs", lambda **k: None)
    monkeypatch.setattr(su, "restore_window", lambda h: None)
    monkeypatch.setattr(session_mod.time, "sleep", lambda x: None)
    monkeypatch.setattr(session_mod, "_safe", lambda f, *a: None)
    orig = SimSession.wait_marker
    monkeypatch.setattr(SimSession, "wait_marker",
                        lambda self, *a, **k: order.append("marker") or orig(self, *a, **k))
    r = s.shot("p1", frame=False, marker="MX_SHOT go", out_dir=str(tmp_path))
    assert order == ["marker", "capture"] and r["marker_line"] == "MX_SHOT go"
    # no marker: unchanged result shape
    assert "marker_line" not in s.shot("p2", frame=False, out_dir=str(tmp_path))


def test_a_missing_marker_fails_the_shot_before_any_capture(tmp_path):
    s = _session(tmp_path)
    (tmp_path / "console.log").write_bytes(b"x\n")
    with pytest.raises(SimError, match="did not appear"):
        s.shot("p", marker="NOPE", marker_timeout_s=0.1)


def test_shot_on_step_shoots_and_reports_failures(monkeypatch):
    s = SimSession.__new__(SimSession)
    kw = {}

    def shot(self, name, **k):
        kw.update(k, name=name)
        return {"path": "/o/" + name + ".png", "marker_line": "L", "anchor": None}
    monkeypatch.setattr(SimSession, "shot", shot)
    r = s._shot_on_step({"kind": "shot_on", "marker": "M", "name": "a", "timeout": 7})
    assert r["ok"] and r["path"] == "/o/a.png" and r["line"] == "L"
    assert kw["marker"] == "M" and kw["marker_timeout_s"] == 7.0 and kw["name"] == "a"

    def boom(self, name, **k):
        raise SimError("marker 'M' did not appear")
    monkeypatch.setattr(SimSession, "shot", boom)
    r = s._shot_on_step({"kind": "shot_on", "marker": "M", "name": "a"})
    assert not r["ok"] and "did not appear" in r["reason"]


def test_reach_runs_shot_on_steps_and_stops_on_a_miss(monkeypatch):
    s = SimSession.__new__(SimSession)
    monkeypatch.setattr(SimSession, "_capture", lambda self: None)
    monkeypatch.setattr(SimSession, "_shot_on_step",
                        lambda self, st: {"ok": st["marker"] != "bad", "path": "/o/x.png",
                                          "reason": "gone"})
    r = s.reach([{"kind": "shot_on", "marker": "a", "name": "x"}])
    assert r["ok"] and r["steps"][0]["path"] == "/o/x.png"
    r = s.reach([{"kind": "shot_on", "marker": "bad", "name": "x"}])
    assert not r["ok"] and "step 1 of 1 (shot_on)" in r["error"]


def test_shot_on_step_schema():
    assert proto.step_errors({"kind": "shot_on", "marker": "M", "name": "a",
                              "timeout": 20}) == []
    assert any("marker" in e for e in proto.step_errors({"kind": "shot_on", "name": "a"}))
    assert any("name" in e for e in proto.step_errors({"kind": "shot_on", "marker": "M"}))
    assert any("timeout" in e for e in proto.step_errors(
        {"kind": "shot_on", "marker": "M", "name": "a", "timeout": 0}))


def test_marker_shot_chunks_are_one_cold_load_then_a_shot_per_marker(tmp_path):
    chunks = jobs.marker_shot_chunks(
        project=str(tmp_path), device="venu3", prg="bin/a.prg", timeout_s=9,
        shots=[{"marker": "MX_SHOT page1", "name": "p1"},
               {"marker": "MX_SHOT page2", "name": "p2", "timeout_s": 30}])
    assert [c.kind for c in chunks] == ["load", "shot", "shot"]
    assert chunks[0].args["force"] is True
    assert chunks[1].args["marker"] == "MX_SHOT page1"
    assert chunks[1].args["marker_timeout_s"] == 9.0
    assert chunks[2].args["marker_timeout_s"] == 30.0
    assert chunks[2].max_s >= 30 + jobs.SHOT_MAX_S           # budget covers the wait
    jobs.make_job(app="a", device="venu3", chunks=chunks)    # validates


def test_screen_chunks_with_a_marker_force_a_cold_load(tmp_path):
    c = jobs.screen_chunks(project=str(tmp_path), device="venu3", name="n",
                           prg="bin/a.prg", marker="MX", marker_timeout_s=5)
    assert c[0].args["force"] is True and c[-1].args["marker"] == "MX"
    plain = jobs.screen_chunks(project=str(tmp_path), device="venu3", name="n",
                               prg="bin/a.prg")
    assert "force" not in plain[0].args and "marker" not in plain[-1].args


def test_reach_budget_covers_shot_on_waits():
    base = jobs.reach_chunk(screen="s", steps=[{"kind": "tap"}])
    waits = jobs.reach_chunk(screen="s", steps=[
        {"kind": "tap"}, {"kind": "shot_on", "marker": "M", "name": "a", "timeout": 100}])
    assert waits.max_s >= base.max_s + 100
    jobs.reach_chunk(screen="s", steps=[{"kind": "shot_on", "timeout": "x"}])  # no crash


def test_broker_forwards_marker_args_to_shot():
    calls = []

    class Rec:
        def shot(self, name, **kw):
            calls.append((name, kw))
            return {"ok": True}
    b = Broker(session=Rec())
    b._dispatch(Job(app="a", chunks=[]), Chunk(ChunkKind.SHOT, {
        "name": "n", "marker": "M", "marker_timeout_s": 3, "bogus": 1}))
    assert calls == [("n", {"marker": "M", "marker_timeout_s": 3})]


def test_validate_job_rejects_a_blank_marker():
    bad = Chunk(ChunkKind.SHOT, {"name": "n", "marker": ""})
    assert any("marker" in e for e in proto.validate_job(Job(app="a", chunks=[bad])))
    bad = Chunk(ChunkKind.SHOT, {"name": "n", "marker": "M", "marker_timeout_s": 0})
    assert any("marker_timeout_s" in e for e in proto.validate_job(Job(app="a", chunks=[bad])))


# ── CLI wiring ───────────────────────────────────────────────────────────────
def test_cli_parses_fit_on_marker_and_shot_on():
    from ciqkit.sim.client import build_parser
    p = build_parser()
    a = p.parse_args(["shot", "x", "--fit", "/r/a.fit", "--on-marker", "MX",
                      "--marker-timeout", "9"])
    assert (a.fit, a.on_marker, a.marker_timeout) == ("/r/a.fit", "MX", 9.0)
    a = p.parse_args(["shot-on", "--marker", "A B=one", "--marker", "C=two", "--fit", "f"])
    assert a.marker == ["A B=one", "C=two"] and a.fit == "f"
    assert p.parse_args(["nav", "--to", "s", "--fit", "f"]).fit == "f"
    assert p.parse_args(["record", "r", "--fit", "f"]).fit == "f"
