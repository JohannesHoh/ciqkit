"""`button` steps: click a device key's spot on the simulator's device art.

macOS does not deliver a Return keystroke to the app as START, so the physical
key is pressed where the simulator draws it. These pin the mapping from the
device profile's simulator.json (device-image pixels) to the capture and the
screen, the error for an unknown id, hold handling, and the enter/esc routing.
"""
import json

import pytest

pytest.importorskip("PIL", reason="Pillow not installed")
pytest.importorskip("numpy", reason="numpy not installed")

from PIL import Image, ImageDraw  # noqa: E402

from ciqkit.capture import framing  # noqa: E402
from ciqkit.sim import session as session_mod  # noqa: E402
from ciqkit.sim.session import SimError, SimSession  # noqa: E402

DEV = "fixture-watch"
KEYS = [
    {"behavior": "onSelect", "id": "enter",
     "location": {"x": 160, "y": 100, "width": 20, "height": 40}},
    {"behavior": "previousPage", "id": "up",
     "location": {"x": 10, "y": 120, "width": 20, "height": 30}},
    {"behavior": "onMenu", "id": "menu", "isHold": True,
     "location": {"x": 10, "y": 120, "width": 20, "height": 30}},
    {"behavior": "onBack", "id": "esc",
     "location": {"x": 160, "y": 180, "width": 20, "height": 40}},
]


@pytest.fixture
def sim_json(tmp_path):
    img = Image.new("RGB", (200, 300), (255, 255, 255))
    ImageDraw.Draw(img).rectangle((20, 30, 170, 280), fill=(20, 20, 20))
    img.save(tmp_path / "device.png")
    p = tmp_path / "simulator.json"
    p.write_text(json.dumps({
        "image": "device.png", "keys": KEYS,
        "display": {"location": {"x": 40, "y": 60, "width": 100, "height": 100}}}))
    return str(p)


def _capture(scale, ox=7, oy=5):
    """The device art as the sim draws it: body at (20,30)-(170,280), scaled,
    inside a larger canvas offset by (ox, oy)."""
    img = Image.new("RGB", (int(200 * scale) + 40, int(300 * scale) + 40),
                    (255, 255, 255))
    ImageDraw.Draw(img).rectangle(
        (ox + 20 * scale, oy + 30 * scale, ox + 171 * scale - 1, oy + 281 * scale - 1),
        fill=(20, 20, 20))
    return img


@pytest.mark.parametrize("scale", [1, 2])
def test_device_point_maps_through_the_dark_silhouette(sim_json, scale):
    img = _capture(scale)
    x, y = framing.sim_device_point(img, DEV, 100, 150, sim_json)
    assert (x, y) == pytest.approx((7 + 100 * scale, 5 + 150 * scale), abs=1)


def test_button_point_is_the_centre_of_the_key_rect(sim_json):
    x, y, key = framing.sim_button_point(_capture(2), DEV, "enter", sim_json)
    assert key["id"] == "enter" and not key["hold"]
    assert (x, y) == pytest.approx((7 + 170 * 2, 5 + 120 * 2), abs=1)


def test_unknown_button_lists_the_available_ids(sim_json):
    with pytest.raises(ValueError) as e:
        framing.sim_button_point(_capture(1), DEV, "lap", sim_json)
    msg = str(e.value)
    assert "'lap'" in msg
    for k in ("enter", "esc", "menu", "up"):
        assert k in msg


def test_device_without_keys_says_so(tmp_path, sim_json):
    p = json.loads(open(sim_json).read())
    p["keys"] = []
    open(sim_json, "w").write(json.dumps(p))
    with pytest.raises(ValueError, match="lists no physical keys"):
        framing.sim_button_point(_capture(1), DEV, "enter", sim_json)


def test_shared_location_keeps_each_ids_own_hold_flag(sim_json):
    keys = {k["id"]: k for k in framing.sim_device_keys(DEV, sim_json)}
    assert keys["up"]["rect"] == keys["menu"]["rect"]
    assert keys["up"]["hold"] is False and keys["menu"]["hold"] is True


def test_misaligned_capture_is_refused_not_guessed(sim_json):
    assert framing.sim_device_point(_capture(1.2), DEV, 10, 10, sim_json) is None


# --- session wiring ---------------------------------------------------------

def _session(monkeypatch, sim_json):
    s = SimSession.__new__(SimSession)
    s._hwnd, s._canvas, s.current = object(), object(), ("app", DEV)
    pressed, typed = [], []
    monkeypatch.setattr(session_mod.su, "sim_device_keys",
                        lambda dev: framing.sim_device_keys(dev, sim_json))
    monkeypatch.setattr(session_mod.su, "sim_button_point",
                        lambda img, dev, kid, scale=None: framing.sim_button_point(
                            img, dev, kid, sim_json))
    monkeypatch.setattr(SimSession, "_capture", lambda self: _capture(2))
    monkeypatch.setattr(SimSession, "_rebind", lambda self: None)
    monkeypatch.setattr(session_mod.su, "restore_window", lambda h: None)
    monkeypatch.setattr(session_mod.su, "pm_press_point",
                        lambda c, x, y, hold_s=0.0, delay=0: pressed.append(
                            (round(x), round(y), hold_s)))
    monkeypatch.setattr(session_mod.su, "phys_key",
                        lambda h, k, delay=0: typed.append(k))
    monkeypatch.setattr(session_mod.time, "sleep", lambda s: None)
    return s, pressed, typed


def test_button_step_presses_the_key_and_honours_hold(monkeypatch, sim_json):
    s, pressed, _ = _session(monkeypatch, sim_json)
    s._do_input("button", 0, 0, "up", "enter", 0)
    s._do_input("button", 0, 0, "up", "menu", 0)               # key's own isHold
    s._do_input("button", 0, 0, "up", "enter", 0, hold=True)   # explicit
    s._do_input("button", 0, 0, "up", "menu", 0, hold=False)   # explicit off
    assert [p[2] for p in pressed] == [0.0, s.HOLD_S, s.HOLD_S, 0.0]
    assert pressed[0][:2] == pytest.approx((7 + 340, 5 + 240), abs=1)


def test_unknown_button_fails_before_any_input(monkeypatch, sim_json):
    s, pressed, _ = _session(monkeypatch, sim_json)
    with pytest.raises(SimError, match="available: .*enter"):
        s.act("button", button="lap", confirm=False)
    assert pressed == []


@pytest.mark.parametrize("name,btn", [("enter", "enter"), ("return", "enter"),
                                      ("esc", "esc"), ("escape", "esc")])
def test_macos_enter_and_esc_route_through_the_button(monkeypatch, sim_json, name, btn):
    s, pressed, typed = _session(monkeypatch, sim_json)
    monkeypatch.setattr(session_mod.su, "IS_MAC", True)
    s._do_input("key", 0, 0, "up", name, 0)
    assert len(pressed) == 1 and typed == []


def test_arrows_stay_on_the_keyboard(monkeypatch, sim_json):
    s, pressed, typed = _session(monkeypatch, sim_json)
    monkeypatch.setattr(session_mod.su, "IS_MAC", True)
    s._do_input("key", 0, 0, "up", "down", 0)
    assert pressed == [] and typed == ["down"]


def test_enter_falls_back_to_keyboard_off_mac_or_without_the_key(monkeypatch, sim_json):
    s, pressed, typed = _session(monkeypatch, sim_json)
    monkeypatch.setattr(session_mod.su, "IS_MAC", False)
    s._do_input("key", 0, 0, "up", "enter", 0)
    monkeypatch.setattr(session_mod.su, "IS_MAC", True)
    monkeypatch.setattr(session_mod.su, "sim_device_keys", lambda dev: [])
    s._do_input("key", 0, 0, "up", "enter", 0)
    assert pressed == [] and typed == ["enter", "enter"]
