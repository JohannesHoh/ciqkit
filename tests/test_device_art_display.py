"""Where the GLASS is in a device-art capture.

The simulator's device-art mode is the one the store screenshots are actually
taken in, and it is the mode both brightness detectors are worst at: outside
the display lies the black bezel rather than bright chrome, so the chrome
detector sizes itself to the whole body and the near-black detector's
"equator" moves with whatever the screen is showing. Measured on real venu3
captures, that cost ~30 px of vertical offset on an ordinary page, and on a
bright one (a QR block, a big white clock) it lost the display outright: the
capture came back composited inside a SECOND watch body -- a ghost bezel ring
around shrunken, off-centre content.

These tests pin the replacement: the glass located from the sim's own body
measured against the SDK device PNG's body-to-screen ratio, reading no screen
pixel at all.
"""
import pytest

pytest.importorskip("PIL", reason="Pillow not installed")
pytest.importorskip("numpy", reason="numpy not installed")

from PIL import Image, ImageDraw  # noqa: E402

from ciqkit.capture import framing  # noqa: E402
from ciqkit.capture import screenshot_utils as su  # noqa: E402

# --- the fixture device ------------------------------------------------------
# One shape, described once, drawn at 1x as Garmin's SDK PNG and at 2x as the
# simulator's device art. Everything the test asserts is a consequence of the
# two agreeing -- which is exactly the property the production code relies on.
BODY = (20, 80, 280, 340)            # ellipse bbox: 260 wide, centre (150, 210)
STRAP = (110, 10, 190, 410)          # makes the silhouette taller than wide
SCREEN = (75, 135, 225, 285)         # the transparent cut-out: r = 75
TRUE_C = (150, 210, 75)              # screen centre + radius, at 1x
DEVICE = "fixture-watch"
SCALE = 2                            # the sim renders device art at 2x here
DEVICE_RES = 300                     # = the screen diameter at 2x


def _sdk_png(tmp_path, body_fill=(40, 40, 40)):
    """Garmin's device PNG: watch on an OPAQUE white workspace, screen cut out."""
    img = Image.new("RGBA", (300, 420), (255, 255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle(STRAP, fill=(*body_fill, 255))
    d.ellipse(BODY, fill=(*body_fill, 255))
    d.rectangle(SCREEN, fill=(0, 0, 0, 0))
    path = tmp_path / f"{DEVICE}.png"
    img.save(path)
    return path


def _capture(content=None, body_fill=(25, 25, 25), canvas=(250, 250, 250)):
    """The same watch as the simulator draws it: device art at 2x on its white
    canvas, between the sim's title bar and status toolbar.

    The chrome is drawn the way the simulator's really is -- a light bar whose
    CONTENT is dark (title text, a separator rule, toolbar glyphs), leaving the
    window corners as canvas. That matters: corner brightness is how every
    detector here decides it is looking at a simulator window at all.
    """
    img = Image.new("RGB", (600 + 40, 840 + 60), canvas)
    d = ImageDraw.Draw(img)
    d.rectangle([160, 4, 480, 16], fill=(90, 90, 90))        # title text
    d.rectangle([0, 26, 639, 27], fill=(90, 90, 90))         # separator rule
    d.rectangle([0, 872, 639, 873], fill=(90, 90, 90))       # toolbar rule
    d.rectangle([160, 882, 480, 894], fill=(90, 90, 90))     # toolbar glyphs
    off = (20, 30)

    def place(box):
        return [box[0] * SCALE + off[0], box[1] * SCALE + off[1],
                box[2] * SCALE + off[0], box[3] * SCALE + off[1]]

    d.rectangle(place(STRAP), fill=body_fill)
    d.ellipse(place(BODY), fill=body_fill)
    d.rectangle(place(SCREEN), fill=(0, 0, 0))
    if content:
        content(d, place(SCREEN))
    return img


def _true_circle():
    return (TRUE_C[0] * SCALE + 20, TRUE_C[1] * SCALE + 30, TRUE_C[2] * SCALE)


def _qr_block(d, screen):
    """The content class that broke detection: a big white block across the
    middle of the display. It erases the body's equator rows from any
    "longest contiguous dark run" measurement -- the very rows the body centre
    is read from."""
    x0, y0, x1, y1 = screen
    d.rectangle([x0 + 30, y0 + 40, x1 - 30, y1 - 40], fill=(255, 255, 255))


def _bright_clock(d, screen):
    """A light theme: most of the glass is pale."""
    x0, y0, x1, y1 = screen
    d.ellipse([x0 + 6, y0 + 6, x1 - 6, y1 - 6], fill=(242, 242, 236))


@pytest.fixture
def installed_device(tmp_path, monkeypatch):
    png = _sdk_png(tmp_path)
    framing._sdk_metrics_cache.clear()
    monkeypatch.setattr(framing, "_sdk_device_png_path",
                        lambda dev: str(png) if dev == DEVICE else None)
    yield
    framing._sdk_metrics_cache.clear()


def test_the_glass_is_located_from_the_body_not_the_content(installed_device):
    got = su.device_art_screen_circle(_capture(), DEVICE, DEVICE_RES)
    assert got is not None
    cx, cy, r = got
    tx, ty, tr = _true_circle()
    assert abs(cx - tx) <= 3 and abs(cy - ty) <= 3
    assert abs(r - tr) <= 3


@pytest.mark.parametrize("content", [None, _qr_block, _bright_clock],
                         ids=["dark", "qr-block", "bright-clock"])
def test_content_cannot_move_the_glass(installed_device, content):
    """The regression, stated as a property: change everything inside the
    display and the located circle must not move by a pixel. The old
    brightness detectors could not hold this -- a white block across the
    middle is what produced the ghost-ringed store shots."""
    assert (su.device_art_screen_circle(_capture(content), DEVICE, DEVICE_RES)
            == su.device_art_screen_circle(_capture(), DEVICE, DEVICE_RES))


def test_a_light_colorway_is_measured_too(installed_device):
    """venu2s ships ivory: body and strap sit just under the canvas white, so
    the dark mask is blind to them and only the light pass sees a watch."""
    img = _capture(body_fill=(196, 192, 186), canvas=(255, 255, 255))
    got = su.device_art_screen_circle(img, DEVICE, DEVICE_RES)
    assert got is not None
    tx, ty, tr = _true_circle()
    assert abs(got[0] - tx) <= 3 and abs(got[1] - ty) <= 3 and abs(got[2] - tr) <= 3


def test_a_measured_capture_never_consults_the_brightness_detector(installed_device,
                                                                  monkeypatch):
    """The point is not that the measurement is usually better -- it is that
    when it applies, the unstable detector is not in the loop at all. Its
    answer cannot leak back in on the one frame it happens to be wrong."""
    def explode(*a, **k):
        raise AssertionError("brightness detector consulted on device art")
    monkeypatch.setattr(framing, "find_watch_face_auto", explode)
    tx, ty, tr = _true_circle()
    got = su.find_display_circle(_capture(_qr_block), DEVICE, DEVICE_RES)
    assert abs(got[0] - tx) <= 3 and abs(got[1] - ty) <= 3 and abs(got[2] - tr) <= 3


def test_a_capture_that_is_not_device_art_is_not_measured(installed_device):
    """A bare round display on chrome has no body to measure; say so rather
    than invent a circle, so the caller falls back."""
    bare = Image.new("RGB", (700, 700), (235, 235, 235))
    ImageDraw.Draw(bare).ellipse([123, 123, 577, 577], fill=(0, 0, 0))
    assert su.device_art_screen_circle(bare, DEVICE, DEVICE_RES) is None
    assert su.find_display_circle(bare, DEVICE, 454) is not None


def test_an_uninstalled_device_falls_back_instead_of_failing(installed_device):
    """No SDK PNG, no ratio. The caller must still get a circle."""
    img = _capture()
    assert su.device_art_screen_circle(img, "not-installed", DEVICE_RES) is None
    assert su.find_display_circle(img, "not-installed", DEVICE_RES) is not None


def test_no_device_id_keeps_the_old_detector(installed_device):
    """find_display_circle is a superset, not a replacement: callers that
    cannot name the device get exactly what they got before."""
    img = _capture()
    assert su.find_display_circle(img, None, DEVICE_RES) == \
        su.find_watch_face_auto(img, DEVICE_RES)


def test_an_implausible_scale_is_refused(installed_device):
    """The simulator renders at 1x or 2x. A body measurement that implies
    anything else means something overlapped the watch -- fall back rather
    than crop a wrong rectangle."""
    assert su.device_art_screen_circle(_capture(), DEVICE, 777) is None


def test_the_sdk_metrics_are_read_once_per_device(installed_device, monkeypatch):
    """This runs on every capture and every recorded frame; the flood fill
    behind it is not free."""
    calls = []
    real = framing._sdk_device_png_path
    monkeypatch.setattr(framing, "_sdk_device_png_path",
                        lambda dev: (calls.append(dev), real(dev))[1])
    for _ in range(3):
        su.device_art_screen_circle(_capture(), DEVICE, DEVICE_RES)
    assert calls == [DEVICE]


# --- "did the app load?" judged on the display, not the window ---------------

def _loaded_page(d, screen, bright=False):
    """An app UI: a few bright rows in the lower half is what the heuristic
    keys on. `bright` adds the big white block of the QR scene."""
    x0, y0, x1, y1 = screen
    if bright:
        # ~40 % of the glass, the proportion the real BTWF QR scene covers.
        d.rectangle([x0 + 55, y0 + 45, x1 - 55, y1 - 75], fill=(255, 255, 255))
    for i in range(4):
        d.rectangle([x0 + 40, y0 + 200 + i * 20, x1 - 40, y0 + 208 + i * 20],
                    fill=(240, 240, 240))


def test_a_bright_page_still_counts_as_loaded(installed_device):
    """Regression: on a device-art window the sim's white canvas dominates the
    frame, so the whole-window brightness test read the BTWF QR scene as "app
    did not load" — and the push then restarted the simulator and re-detected
    the face, which is how one bad detection got into an otherwise good batch."""
    plain = _capture(lambda d, s: _loaded_page(d, s))
    qr = _capture(lambda d, s: _loaded_page(d, s, bright=True))
    assert su.looks_like_app_loaded(plain, DEVICE, DEVICE_RES) is True
    assert su.looks_like_app_loaded(qr, DEVICE, DEVICE_RES) is True
    # ...and state the bug: judged on the whole window, the bright page loses.
    assert su._looks_like_app_loaded(qr) is False


def test_a_blank_screen_is_still_not_loaded(installed_device):
    """The check must still fail on a watch that is showing nothing."""
    assert su.looks_like_app_loaded(_capture(), DEVICE, DEVICE_RES) is False


def test_without_a_device_the_old_whole_image_test_is_used(installed_device):
    """No measurement, no change: callers that cannot name the device get
    exactly the behaviour they had."""
    img = _capture(lambda d, s: _loaded_page(d, s))
    assert su.looks_like_app_loaded(img) == su._looks_like_app_loaded(img)


# --- the display rect a device profile declares -----------------------------

def _fake_device(root, device="fakewatch"):
    """A device profile: an image with a dark watch silhouette on white and a
    simulator.json declaring where the display sits on it."""
    import json
    from PIL import Image, ImageDraw
    d = root / device
    d.mkdir(parents=True)
    img = Image.new("RGB", (300, 420), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle((100, 10, 200, 409), fill=(30, 30, 30))      # strap
    draw.ellipse((20, 80, 280, 340), fill=(40, 40, 40))          # case
    draw.ellipse((50, 110, 250, 310), fill=(0, 0, 0))            # display
    img.save(d / f"{device}.png")
    (d / "simulator.json").write_text(json.dumps({
        "image": f"{device}.png",
        "display": {"location": {"x": 50, "y": 110, "width": 200, "height": 200}}}))
    return img


def test_the_declared_display_rect_is_found_on_a_cut_off_capture(tmp_path, monkeypatch):
    """The Windows simulator draws the device image 1:1 in a panel that cuts
    the strap off at the bottom; the display must still be located exactly."""
    from PIL import Image
    from ciqkit.capture import framing
    from ciqkit.sim import preflight
    art = _fake_device(tmp_path)
    monkeypatch.setattr(preflight, "devices_root", lambda: str(tmp_path))
    cap = Image.new("RGB", (330, 380), (255, 255, 255))
    cap.paste(art, (12, 7))                                     # bottom cut off
    assert framing.sim_display_box(cap, "fakewatch") == (62, 117, 262, 317)


def test_a_capture_that_is_not_that_device_at_1x_is_refused(tmp_path, monkeypatch):
    from PIL import Image
    from ciqkit.capture import framing
    from ciqkit.sim import preflight
    art = _fake_device(tmp_path)
    monkeypatch.setattr(preflight, "devices_root", lambda: str(tmp_path))
    retina = Image.new("RGB", (700, 900), (255, 255, 255))
    retina.paste(art.resize((600, 840)), (10, 10))
    assert framing.sim_display_box(retina, "fakewatch") is None
    assert framing.sim_display_box(art, "no-such-device") is None
