"""`button` steps: click a device key's spot on the simulator's device art.

macOS does not deliver a Return keystroke to the app as START, so the physical
key is pressed where the simulator draws it. These pin the mapping from the
device profile's simulator.json (device-image pixels) to the capture and the
screen, the error for an unknown id, hold handling, and the enter/esc routing.
"""
import json
import os

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


def test_a_silhouette_that_is_not_the_device_is_refused_not_guessed(sim_json):
    """A dark shape of another outline (a wide bar, not this body) agrees with
    the device silhouette at no scale or offset, so nothing is aimed."""
    img = Image.new("RGB", (300, 360), (255, 255, 255))
    ImageDraw.Draw(img).rectangle((10, 150, 289, 209), fill=(20, 20, 20))
    assert framing.sim_device_point(img, DEV, 10, 10, sim_json) is None


def test_a_view_at_neither_stock_scale_is_refused_not_guessed(sim_json):
    """Neither 1x, 2x nor the hint: no free (silhouette-width) scale is tried,
    because on real device art it let another device's outline clear the
    floor. Nothing is aimed; the press fails with diagnostics instead."""
    assert framing.sim_device_point(_capture(1.5), DEV, 100, 150, sim_json) is None


# --- Windows: the DPI-unaware simulator's own drawing -----------------------
# PrintWindow returns the simulator's 96-DPI drawing, so the device PNG is in
# the capture 1:1 while retina_scale (image px per PHYSICAL px) is 0.5 at
# 200 %. The capture is cropped to the device panel -- no macOS chrome -- and
# the panel is shorter than the art, so the strap end is cut off.

WIN_KEYS = [
    {"behavior": "onSelect", "id": "enter",
     "location": {"x": 262, "y": 150, "width": 14, "height": 40}},
    {"behavior": "onBack", "id": "esc",
     "location": {"x": 262, "y": 260, "width": 14, "height": 40}},
    {"behavior": "onMenu", "id": "strap", "isHold": True,
     "location": {"x": 120, "y": 470, "width": 40, "height": 20}},
]


@pytest.fixture
def win_sim_json(tmp_path):
    """A round watch with a strap and two side buttons, 280x520, on white:
    built here, not Garmin's art."""
    img = Image.new("RGB", (280, 520), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle((90, 0, 190, 519), fill=(40, 40, 40))            # strap
    d.ellipse((20, 110, 260, 350), fill=(25, 25, 25))            # case
    d.rectangle((260, 150, 275, 189), fill=(60, 60, 60))         # enter
    d.rectangle((260, 260, 275, 299), fill=(60, 60, 60))         # esc
    d.ellipse((50, 140, 230, 320), fill=(0, 0, 0))               # display
    img.save(tmp_path / "device.png")
    p = tmp_path / "simulator.json"
    p.write_text(json.dumps({
        "image": "device.png", "keys": WIN_KEYS,
        "display": {"location": {"x": 50, "y": 140, "width": 180, "height": 180}}}))
    return str(p), img


def _win_capture(art, ox=3, oy=0, panel=(300, 420), screen=(250, 250, 250)):
    """The panel as PrintWindow hands it back: the art 1:1 at (ox, oy), its
    bottom past the panel's edge, an app drawing a bright screen."""
    cap = Image.new("RGB", panel, (255, 255, 255))
    cap.paste(art, (ox, oy))
    ImageDraw.Draw(cap).ellipse((ox + 50, oy + 140, ox + 230, oy + 320), fill=screen)
    return cap


@pytest.mark.parametrize("hint", [0.5, 1.0, None])
def test_windows_capture_aims_at_the_button_despite_the_retina_hint(win_sim_json, hint):
    path, art = win_sim_json
    cap = _win_capture(art)
    x, y, key = framing.sim_button_point(cap, DEV, "enter", path, scale=hint)
    assert key["id"] == "enter"
    assert (x, y) == pytest.approx((3 + 269, 0 + 170), abs=1)
    x, y, _ = framing.sim_button_point(cap, DEV, "esc", path, scale=hint)
    assert (x, y) == pytest.approx((3 + 269, 280), abs=1)


def test_windows_capture_with_a_dark_screen_still_aligns(win_sim_json):
    path, art = win_sim_json
    cap = _win_capture(art, ox=11, oy=4, screen=(5, 5, 5))
    x, y, _ = framing.sim_button_point(cap, DEV, "enter", path, scale=0.5)
    assert (x, y) == pytest.approx((11 + 269, 4 + 170), abs=1)


def test_too_little_of_the_art_in_view_is_refused(win_sim_json):
    path, art = win_sim_json
    cap = _win_capture(art, panel=(300, 200))          # under half the art
    assert framing.sim_device_point(cap, DEV, 10, 10, path) is None


def _win_session(monkeypatch, path, cap, tmp_path):
    s = SimSession.__new__(SimSession)
    s._hwnd, s.current, s.out_dir = object(), ("app", DEV), str(tmp_path / "out")
    s._canvas = type("C", (), {"retina_scale": 0.5, "content_left": 8,
                               "content_top": 31, "content_w": cap.width,
                               "content_h": cap.height, "display_box": None})()
    pressed = []
    monkeypatch.setattr(session_mod.su, "IS_WIN", True)
    monkeypatch.setattr(session_mod.su, "sim_device_keys",
                        lambda dev: framing.sim_device_keys(dev, path))
    monkeypatch.setattr(session_mod.su, "sim_button_point",
                        lambda img, dev, kid, scale=None: framing.sim_button_point(
                            img, dev, kid, path, scale))
    monkeypatch.setattr(session_mod.su, "explain_device_alignment",
                        lambda img, dev, od, scale=None: framing.explain_device_alignment(
                            img, dev, od, scale, path))
    monkeypatch.setattr(SimSession, "_capture", lambda self: cap)
    monkeypatch.setattr(session_mod.su, "pm_press_point",
                        lambda c, x, y, hold_s=0.0, delay=0: pressed.append(
                            (round(x), round(y), hold_s)))
    return s, pressed


def test_windows_session_presses_enter_on_the_art(monkeypatch, win_sim_json, tmp_path):
    path, art = win_sim_json
    s, pressed = _win_session(monkeypatch, path, _win_capture(art), tmp_path)
    s._press_button("enter", None, 0)
    assert pressed == [(272, 170, 0.0)]


def test_windows_button_below_the_panel_is_refused(monkeypatch, win_sim_json, tmp_path):
    path, art = win_sim_json
    s, pressed = _win_session(monkeypatch, path, _win_capture(art), tmp_path)
    with pytest.raises(SimError, match="outside the visible simulator panel"):
        s._press_button("strap", None, 0)
    assert pressed == []


def test_failed_alignment_saves_its_data_and_warns(monkeypatch, win_sim_json,
                                                   tmp_path, caplog):
    path, _art = win_sim_json
    blank = Image.new("RGB", (300, 420), (255, 255, 255))
    s, pressed = _win_session(monkeypatch, path, blank, tmp_path)
    with caplog.at_level("WARNING", logger="ciqkit.sim"):
        with pytest.raises(SimError, match="alignment data in") as e:
            s._press_button("enter", None, 0)
    assert pressed == []
    od = str(e.value).rsplit("alignment data in ", 1)[1]
    # silhouettes only: the raw capture holds Garmin's art as the sim draws it
    assert not os.path.exists(os.path.join(od, "capture.png"))
    for f in ("capture-mask.png", "device-mask.png",
              "alignment.json", "geometry.json"):
        assert (tmp_path / "out" / "button-align").exists()
        assert os.path.isfile(os.path.join(od, f)), f
    info = json.loads(open(os.path.join(od, "alignment.json")).read())
    assert info["scale_hint"] == 1.0 and info["tried"]
    geo = json.loads(open(os.path.join(od, "geometry.json")).read())
    assert geo["retina_scale"] == 0.5 and geo["platform"] == "windows"
    msg = " ".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    assert "scale hint 1.0" in msg and "retina_scale" in msg and od in msg


def test_every_stock_scale_is_searched_and_the_best_fit_wins(win_sim_json):
    """A passable fit at the hint must not end the search before the exact
    one at another stock scale: the aim follows the better score."""
    path, art = win_sim_json
    cap = Image.new("RGB", (400, 640), (255, 255, 255))
    cap.paste(art, (5, 2))
    best, tried, _, _ = framing._align_device_art(cap, path, 0.5)
    assert best[3] == 1.0 and [t[0] for t in tried][:2] == [0.5, 1.0]


def test_alignment_overlay_carries_no_capture_pixels(win_sim_json, tmp_path):
    path, art = win_sim_json
    cap = _win_capture(art, screen=(10, 200, 30))
    od = str(tmp_path / "diag")
    info = framing.explain_device_alignment(cap, DEV, od, 1.0, path)
    assert "capture" not in info["files"]
    over = Image.open(info["files"]["overlay"]).convert("RGB")
    colours = {c for _, c in over.getcolors(maxcolors=1 << 20)}
    assert colours <= {(255, 255, 255), (0, 160, 0), (230, 0, 0)}


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
