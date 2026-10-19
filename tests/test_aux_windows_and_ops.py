"""Step schema (id/button), simulator aux windows, crash dialogs, phone link.

All fixtures: the AppleScript output is fed in as text, so none of this needs a
simulator, a GUI or macOS.
"""
import pytest

from ciqkit.capture import screenshot_utils as su
from ciqkit.capture import simwindows as sw
from ciqkit.sim import broker as broker_mod
from ciqkit.sim import protocol as proto
from ciqkit.sim import session as session_mod
from ciqkit.sim.broker import Broker
from ciqkit.sim.protocol import Chunk, ChunkKind, Job

broker_mod.leaselib = None
FS = sw.FS


# ── 1. one step schema ───────────────────────────────────────────────────────
def test_button_id_accepts_id_and_alias():
    assert proto.button_id({"kind": "button", "id": "enter"}) == "enter"
    assert proto.button_id({"kind": "button", "button": "enter"}) == "enter"
    assert proto.button_id({"id": "esc", "button": "esc"}) == "esc"
    assert proto.button_id({"kind": "tap"}) is None
    assert proto.button_id({"id": "", "button": "up"}) == "up"


@pytest.mark.parametrize("step", [{"kind": "button", "id": "enter"},
                                  {"kind": "button", "button": "enter"},
                                  {"kind": "tap", "x": 0.5, "y": 0.4},
                                  {"kind": "phone", "state": "connected"},
                                  {"kind": "close_aux"}])
def test_step_errors_accepts(step):
    assert proto.step_errors(step) == []


@pytest.mark.parametrize("step,frag", [
    ({"kind": "button"}, "needs an id"),
    ({"kind": "button", "id": "a", "button": "b"}, "disagree"),
    ({"kind": "wiggle"}, "unknown kind"),
    ({"kind": "phone"}, "needs state"),
    ({"kind": "phone", "state": "maybe"}, "needs state"),
    ("tap", "must be an object")])
def test_step_errors_rejects(step, frag):
    assert any(frag in e for e in proto.step_errors(step))


def _job(chunk):
    return Job(app="a", chunks=[chunk])


@pytest.mark.parametrize("key", ["id", "button"])
def test_validate_job_accepts_both_spellings_on_act_and_reach(key):
    act = Chunk(ChunkKind.ACT, {"kind": "button", key: "enter"})
    reach = Chunk(ChunkKind.REACH, {"steps": [{"kind": "button", key: "enter"}]})
    assert proto.validate_job(_job(act)) == []
    assert proto.validate_job(_job(reach)) == []


def test_validate_job_rejects_a_bad_step_at_submit():
    bad = Chunk(ChunkKind.REACH, {"steps": [{"kind": "tap"}, {"kind": "button"}]})
    errs = proto.validate_job(_job(bad))
    assert len(errs) == 1 and "step 2" in errs[0]
    assert proto.validate_job(_job(Chunk(ChunkKind.ACT, {"kind": "button"})))


class _Rec:
    def __init__(self):
        self.calls = []

    def act(self, kind, **kw):
        self.calls.append((kind, kw))
        return {"ok": True}


@pytest.mark.parametrize("args,want", [
    ({"kind": "button", "id": "enter"}, "enter"),
    ({"kind": "button", "button": "enter"}, "enter")])
def test_broker_act_forwards_either_spelling_as_button(args, want):
    rec = _Rec()
    b = Broker(session=rec)
    b._dispatch(Job(app="a", chunks=[]), Chunk(ChunkKind.ACT, args))
    assert rec.calls == [("button", {"button": want})]


def test_broker_act_forwards_phone_state():
    rec = _Rec()
    b = Broker(session=rec)
    b._dispatch(Job(app="a", chunks=[]),
                Chunk(ChunkKind.ACT, {"kind": "phone", "state": "disconnected"}))
    assert rec.calls == [("phone", {"state": "disconnected"})]


def test_reach_passes_id_or_alias_to_act(monkeypatch):
    s = session_mod.SimSession.__new__(session_mod.SimSession)
    seen = []
    monkeypatch.setattr(session_mod.SimSession, "act",
                        lambda self, kind, *a, **k: seen.append((kind, k.get("button")))
                        or {"ok": False, "reason": "stop"})
    for step in ({"kind": "button", "id": "enter"}, {"kind": "button", "button": "esc"}):
        s.reach([step])
    assert seen == [("button", "enter"), ("button", "esc")]


# ── 2. aux windows ───────────────────────────────────────────────────────────
def _line(name, sub="AXStandardWindow", x=0, y=0, w=300, h=300, btns=""):
    return FS.join([name, sub, str(x), str(y), str(w), str(h), btns])


LISTING = "\n".join([
    _line("Webpage Window", w=500, h=700, x=10, y=10),        # frontmost: item 1
    _line("CIQ Simulator - Venu® 3 (5.2.0)", x=153, y=30, w=636, h=938),
    _line("Error", sub="AXDialog", btns="OK,"),
])


def test_device_window_is_found_by_title_not_by_position():
    c = sw.classify(sw.parse_windows(LISTING))
    assert c.device.name.startswith("CIQ Simulator") and c.device.index == 2
    assert [w.name for w in c.aux] == ["Webpage Window"]
    assert [w.name for w in c.dialogs] == ["Error"]


def test_device_falls_back_to_the_largest_window():
    c = sw.classify(sw.parse_windows("\n".join(
        [_line("Panel", w=200, h=200), _line("Watch", w=600, h=900)])))
    assert c.device.name == "Watch" and [w.name for w in c.aux] == ["Panel"]


def test_find_sim_window_ignores_an_aux_window_in_front(monkeypatch):
    monkeypatch.setattr(su, "IS_MAC", True)
    monkeypatch.setattr(su, "IS_WIN", False)
    monkeypatch.setattr(su, "_mac_sim_windows",
                        lambda: sw.parse_windows(LISTING))
    w = su.find_sim_window()
    assert (w.left, w.top, w.width, w.height) == (153, 30, 636, 938)


def test_close_aux_closes_only_non_device_non_dialog_windows(monkeypatch):
    monkeypatch.setattr(su, "IS_MAC", True)
    monkeypatch.delenv("CIQKIT_KEEP_AUX", raising=False)
    monkeypatch.setattr(su, "_mac_sim_windows", lambda: sw.parse_windows(LISTING))
    scripts = []

    class R:
        stdout = "ok\n"
    monkeypatch.setattr(su.subprocess, "run",
                        lambda cmd, **k: scripts.append(cmd[-1]) or R())
    monkeypatch.setattr(su.time, "sleep", lambda s: None)
    su.drain_dismissed()
    assert su.close_aux_windows() == ["Webpage Window"]
    assert len(scripts) == 1 and "window 1" in scripts[0]
    ev = su.drain_dismissed()
    assert ev[0]["kind"] == "aux_window_closed" and ev[0]["name"] == "Webpage Window"


def test_close_aux_can_be_disabled(monkeypatch):
    monkeypatch.setattr(su, "IS_MAC", True)
    monkeypatch.setenv("CIQKIT_KEEP_AUX", "1")
    monkeypatch.setattr(su, "_mac_sim_windows", lambda: pytest.fail("listed"))
    assert su.close_aux_windows() == []


# ── 3. crash-reporter dialogs ────────────────────────────────────────────────
def _scan(pn, wi, texts, btns):
    return FS.join([pn, str(wi), "|".join(texts) + "|", "|".join(btns) + "|"])


@pytest.mark.parametrize("text", [
    "ConnectIQ quit unexpectedly.",
    "“Connect IQ” quit unexpectedly.",
    "The application “simulator” quit unexpectedly",
    "WebKit Web Content quit unexpectedly",
    "monkeydo closed because of a problem"])
def test_crash_dialog_recognised_for_simulator_processes(text):
    assert sw.match_crash_dialog([text]) is not None


@pytest.mark.parametrize("text", ["Safari quit unexpectedly.",
                                  "Do you want to save the changes?", ""])
def test_other_apps_crashes_are_left_alone(text):
    assert sw.match_crash_dialog([text]) is None


def test_crash_dialog_is_dismissed_with_a_safe_button_and_recorded():
    su.drain_dismissed()
    scan = _scan("UserNotificationCenter", 1,
                 ["ConnectIQ quit unexpectedly.", "Click Reopen to open the app again."],
                 ["Report…", "Reopen", "OK"])
    assert su._plan_alert_clicks(scan) == [("UserNotificationCenter", 1, "OK")]
    ev = su.drain_dismissed()
    assert ev[0]["kind"] == "simulator_crash_dialog"
    assert "ConnectIQ" in ev[0]["process"] and ev[0]["clicked"] == "OK"


def test_crash_dialog_without_a_safe_button_is_not_clicked():
    scan = _scan("CrashReporter", 1, ["ConnectIQ quit unexpectedly."],
                 ["Report…", "Reopen"])
    assert su._plan_alert_clicks(scan) == []


def test_unrelated_crash_dialog_is_not_touched():
    scan = _scan("UserNotificationCenter", 1, ["Safari quit unexpectedly."], ["OK"])
    su.drain_dismissed()
    assert su._plan_alert_clicks(scan) == []
    assert su.drain_dismissed() == []


def test_adb_and_tcc_alerts_still_get_ok_or_allow():
    scan = "\n".join([
        _scan("UserNotificationCenter", 1, ["There is no data connection"], ["OK"]),
        _scan("UserNotificationCenter", 2, ["node would like to access data"],
              ["Don't Allow", "Allow"])])
    assert su._plan_alert_clicks(scan) == [("UserNotificationCenter", 2, "Allow"),
                                           ("UserNotificationCenter", 1, "OK")]


def test_dismissed_events_land_on_the_step_result(monkeypatch):
    s = session_mod.SimSession.__new__(session_mod.SimSession)

    def fake(self, *a, **k):
        su.note_dismissed("simulator_crash_dialog", process="ConnectIQ")
        return {"ok": True}
    monkeypatch.setattr(session_mod.SimSession, "_wd", fake, raising=False)
    wrapped = session_mod._with_dismissed(fake)
    r = wrapped(s)
    assert r["simulator_crashed"] is True
    assert r["dismissed"][0]["process"] == "ConnectIQ"
    assert "dismissed" not in session_mod._with_dismissed(
        lambda self: {"ok": True})(s)


# ── 4. phone connection ──────────────────────────────────────────────────────
@pytest.mark.parametrize("v,item", [(True, "Connected"), (False, "Not Connected"),
                                    ("connected", "Connected"),
                                    ("disconnected", "Not Connected"),
                                    ("not_connected", "Not Connected")])
def test_phone_menu_item(v, item):
    assert sw.phone_menu_item(v) == item


def test_phone_menu_item_rejects_unknown():
    with pytest.raises(ValueError):
        sw.phone_menu_item("sideways")


def test_parse_marked_item():
    text = FS.join(["Not Initialized", ""]) + "\n" + FS.join(["Not Connected", "✓"]) \
        + "\n" + FS.join(["Connected", ""])
    assert sw.parse_marked_item(text) == "Not Connected"
    assert sw.parse_marked_item(FS.join(["Connected", "missing value"])) is None


def test_set_phone_connection_clicks_then_verifies(monkeypatch):
    monkeypatch.setattr(su, "IS_MAC", True)
    state = {"v": "Connected"}
    monkeypatch.setattr(su, "read_phone_connection", lambda: state["v"])
    clicks = []

    class R:
        stdout = ""
    monkeypatch.setattr(su.subprocess, "run",
                        lambda cmd, **k: (clicks.append(cmd[-1]),
                                          state.update(v="Not Connected"), R())[2])
    monkeypatch.setattr(su.time, "sleep", lambda s: None)
    r = su.set_phone_connection(False)
    assert r == {"ok": True, "requested": "Not Connected", "state": "Not Connected",
                 "changed": True, "previous": "Connected"}
    assert 'click menu item "Not Connected"' in clicks[0]
    # already there: no click
    clicks.clear()
    assert su.set_phone_connection("disconnected")["changed"] is False
    assert clicks == []


def test_set_phone_reports_a_menu_that_did_not_change(monkeypatch):
    monkeypatch.setattr(su, "IS_MAC", True)
    monkeypatch.setattr(su, "read_phone_connection", lambda: "Connected")
    monkeypatch.setattr(su.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(su.time, "sleep", lambda s: None)
    s = session_mod.SimSession.__new__(session_mod.SimSession)
    r = s.set_phone("disconnected")
    assert r["ok"] is False and "wanted" in r["reason"]
    assert r["phoneConnected"] is True


def test_session_act_routes_ops_without_touching_the_gui(monkeypatch):
    s = session_mod.SimSession.__new__(session_mod.SimSession)
    monkeypatch.setattr(su, "set_phone_connection",
                        lambda st: {"ok": True, "requested": "Connected",
                                    "state": "Connected", "changed": False})
    r = s.act("phone", state="connected")
    assert r["ok"] and r["phoneConnected"] is True
    monkeypatch.setattr(su, "dismiss_modal_dialogs", lambda verbose=False: 0)
    monkeypatch.setattr(su, "IS_MAC", False)
    assert s.act("close_aux")["ok"] is True


def test_real_macos_crash_dialog_gets_ignore_not_reopen_or_report():
    """Measured on macOS 26 / SDK 9.1.0: presented by UserNotificationCenter,
    buttons Reopen / Ignore / Report... (no OK)."""
    su.drain_dismissed()
    scan = _scan("UserNotificationCenter", 1,
                 ["Connect IQ Device Simulator quit unexpectedly.",
                  "Click Reopen to open the application again. Click Report to "
                  "see more detailed information and send a report to Apple."],
                 ["Reopen", "Ignore", "Report…"])
    assert su._plan_alert_clicks(scan) == [("UserNotificationCenter", 1, "Ignore")]
    ev = su.drain_dismissed()[0]
    assert ev["process"] == "Connect IQ Device Simulator" and ev["clicked"] == "Ignore"
