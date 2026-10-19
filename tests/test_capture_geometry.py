"""Face detection, cropping, framing and frame signatures — all headless.

These are the pure-image half of `screenshot_utils`: no window, no simulator,
no SDK. They are also where the subtle failures live, because every one of them
feeds a decision the broker makes silently — is this the app or the watchface,
did the screen change, is this crop the display or the whole watch body. A
wrong answer here does not raise; it produces a confidently wrong screenshot.

The window-driving half (`ensure_simulator`, `pm_click`, …) cannot be covered
without a real macOS simulator and is exercised by hand. The one exception is
`capture_window`, whose PREAMBLE -- which of the window-raising steps run
before the pixels are read -- is pinned headlessly in
test_capture_window_preamble.py by stubbing the platform calls.
"""
import sys

import pytest

pytest.importorskip("PIL", reason="Pillow not installed")
pytest.importorskip("numpy", reason="numpy not installed")

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from ciqkit.capture import screenshot_utils as su  # noqa: E402

# A simulator window: light chrome, with the round display drawn on it.
#
# The display fill must be TRUE black: find_watch_face keys on `< DARK_THRESH`
# (5), matching a real OLED watch face. A "nearly black" fixture silently fails
# detection and would make these tests assert nothing.
WIN = 700
FACE_R = 227          # 454-wide display at 1x
CX = CY = WIN // 2


def sim_window(face_fill=(0, 0, 0), chrome=(235, 235, 235),
               cx=CX, cy=CY, r=FACE_R, content=None):
    img = Image.new("RGB", (WIN, WIN), chrome)
    d = ImageDraw.Draw(img)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=face_fill)
    if content:
        content(d)
    return img


def _gray(img):
    return np.mean(np.array(img)[:, :, :3].astype(np.float32), axis=2)


# --- SDK path handling -------------------------------------------------------

def test_sdk_bin_builds_a_path_under_the_configured_sdk(tmp_path):
    su.init(str(tmp_path), ".")
    got = su.sdk_bin("monkeydo")
    assert got.startswith(str(tmp_path))
    assert got.endswith("monkeydo.bat" if sys.platform == "win32" else "monkeydo")


def test_a_missing_device_png_yields_no_path():
    assert su._sdk_device_png_path("definitely-not-a-device") is None


# --- corner brightness: the chrome-vs-content signal -------------------------

def test_corner_brightness_reads_the_chrome_not_the_face():
    bright = su._corner_brightness(_gray(sim_window(chrome=(240, 240, 240))))
    dark = su._corner_brightness(_gray(sim_window(chrome=(5, 5, 5))))
    assert bright > 200
    assert dark < 30


# --- face detection ----------------------------------------------------------

def test_a_dark_round_face_on_light_chrome_is_found():
    r = su.find_watch_face(sim_window(), device_res=454)
    assert r is not None
    cx, cy, rad = r
    assert abs(cx - CX) <= 6 and abs(cy - CY) <= 6
    assert abs(rad - FACE_R) <= 12


def test_detection_survives_bright_content_on_the_face():
    """A white page inside the display must not shrink or move the circle —
    the failure that produced ghost-ringed store shots."""
    def bright(d):
        d.rectangle([CX - 120, CY - 120, CX + 120, CY + 120], fill=(255, 255, 255))
    r = su.find_watch_face_auto(sim_window(content=bright), device_res=454)
    assert r is not None
    cx, cy, rad = r
    assert abs(cx - CX) <= 10 and abs(cy - CY) <= 10
    assert abs(rad - FACE_R) <= 20


def test_an_off_centre_face_is_located_where_it_actually_is():
    off_x, off_y = CX - 60, CY + 40
    r = su.find_watch_face(sim_window(cx=off_x, cy=off_y), device_res=454)
    assert r is not None
    assert abs(r[0] - off_x) <= 8 and abs(r[1] - off_y) <= 8


def test_a_uniform_image_has_no_face():
    assert su.find_watch_face(Image.new("RGB", (WIN, WIN), (128, 128, 128))) is None


def test_face_fit_score_prefers_the_true_circle_over_a_wrong_one():
    img = sim_window()
    g, corner = _gray(img), su._corner_brightness(_gray(img))
    good = su.face_fit_score(g, corner, CX, CY, FACE_R)
    too_big = su.face_fit_score(g, corner, CX, CY, int(FACE_R * 1.6))
    assert good > too_big


# --- cropping ----------------------------------------------------------------

def test_crop_face_returns_a_square_of_the_display():
    out = su.crop_face(sim_window(), CX, CY, FACE_R)
    w, h = out.size
    assert w == h
    assert abs(w - 2 * FACE_R) <= 8


def test_crop_face_resizes_to_the_native_resolution():
    assert su.crop_face(sim_window(), CX, CY, FACE_R, native_res=454).size == (454, 454)


def test_crop_watch_body_is_larger_than_the_display():
    body = su.crop_watch_body(sim_window(), CX, CY, FACE_R)
    assert body.size[0] >= 2 * FACE_R
    assert body.size[1] >= 2 * FACE_R


# --- framing -----------------------------------------------------------------

def test_framing_produces_a_square_image_on_the_store_background():
    face = su.crop_face(sim_window(), CX, CY, FACE_R)
    framed = su.make_framed_screenshot(face, size=400)
    assert framed.size == (400, 400)
    corner = framed.convert("RGB").getpixel((2, 2))
    assert max(abs(a - b) for a, b in zip(corner, su.STORE_BG_COLOR, strict=True)) < 40


def test_sdk_framing_declines_an_unknown_device_instead_of_guessing():
    """Returning None is what lets the caller fall back to a plain crop."""
    face = su.crop_face(sim_window(), CX, CY, FACE_R)
    assert su.make_sdk_framed_screenshot(face, "definitely-not-a-device") is None


def test_device_art_framing_declines_a_bare_display():
    """A plain screen capture has no watch body to trim, so this must return
    None and let the normal SDK framer run."""
    bare = Image.new("RGB", (454, 454), (10, 10, 10))
    assert su.frame_device_art_capture(bare) is None


# --- device-art framing: detect once, apply to every frame -------------------

def device_art_window(content=None):
    """A device-art sim capture: the sim drew the whole watch (strap + body +
    round screen) on a white canvas. Taller than wide, dark on white."""
    W, H = 700, 900
    img = Image.new("RGB", (W, H), (250, 250, 250))
    d = ImageDraw.Draw(img)
    d.rectangle([300, 120, 400, 780], fill=(30, 30, 30))    # strap
    d.ellipse([170, 270, 530, 630], fill=(20, 20, 20))      # body
    d.ellipse([200, 300, 500, 600], fill=(0, 0, 0))         # screen
    if content:
        content(d)
    return img


def _bright_screen(d):
    # A wide bright band across the screen -- the content class that breaks
    # per-frame detection (it splits the dark row runs).
    d.rectangle([230, 330, 470, 470], fill=(255, 255, 255))


def test_the_device_art_split_matches_the_fused_framer():
    """frame_device_art_capture must stay bit-identical: it is the store-image
    path, and the split (detect + apply) exists only so record() can reuse it."""
    img = device_art_window()
    box = su.detect_device_art_geometry(img)
    assert box is not None
    fused = su.frame_device_art_capture(img)
    split = su.apply_device_art_frame(img, box)
    assert (np.asarray(fused) == np.asarray(split)).all()


def test_device_art_detection_is_content_dependent_which_is_why_it_runs_once():
    """Bright screen content splits the dark row runs and moves the detected
    box -- the frame-to-frame wobble that detect-once exists to prevent."""
    plain = su.detect_device_art_geometry(device_art_window())
    bright = su.detect_device_art_geometry(device_art_window(_bright_screen))
    assert plain is not None and bright is not None
    assert plain != bright


def test_one_detected_box_frames_every_content_variant_identically():
    """Apply the SAME box to frames whose screen content differs: the output
    size and the watch's position on the canvas must not move at all."""
    box = su.detect_device_art_geometry(device_art_window())
    outs = [su.apply_device_art_frame(device_art_window(c), box)
            for c in (None, _bright_screen)]
    assert outs[0].size == outs[1].size == (su.STORE_SCREENSHOT_SIZE,) * 2
    # The watch body's dark-pixel bounding box is the framing: it must be
    # pixel-identical across frames even though the screen content changed.
    boxes = []
    for out in outs:
        a = np.asarray(out).min(axis=2)
        ys, xs = np.where(a < 100)
        boxes.append((xs.min(), ys.min(), xs.max(), ys.max()))
    assert boxes[0] == boxes[1]


def light_device_art_window(content=None):
    """The SAME device art in a LIGHT colorway — venu2s ships ivory: cream
    band, pale body, gold bezel, all ~190 against a pure-white canvas. Only
    the glass is dark."""
    W, H = 700, 900
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([300, 120, 400, 780], fill=(196, 192, 186))   # cream strap
    d.ellipse([170, 270, 530, 630], fill=(205, 195, 150))     # pale gold body
    d.ellipse([200, 300, 500, 600], fill=(0, 0, 0))           # screen
    if content:
        content(d)
    return img


def test_a_light_colorway_is_still_recognised_as_device_art():
    """Regression: venu2s (ivory band, gold bezel) read as a bare round display
    because the dark-pixel mask could not see its strap, so the capture went
    down the SDK framer and came out double-framed — a ghost bezel ring around
    shrunken content. The box must span the strap, not just the body."""
    box = su.detect_device_art_geometry(light_device_art_window())
    assert box is not None
    x0, y0, x1, y1 = box
    assert y1 - y0 > (x1 - x0) * 1.15     # the strap is in the crop
    assert y0 < 160 and y1 > 740          # both strap ends, not just the body


def test_a_light_colorway_frames_without_a_second_body():
    """The device-art path trims the sim's own watch onto the dark background;
    nothing may be re-composited onto an SDK body."""
    out = su.frame_device_art_capture(light_device_art_window())
    assert out is not None and out.size == (su.STORE_SCREENSHOT_SIZE,) * 2


def test_the_margin_stops_at_the_sim_chrome():
    """Regression: the breathing-room margin walked back into the sim's title
    bar and bottom toolbar — the runs the edge filter had just dropped — and
    stranded their separator rules across the top and bottom of the store shot.
    On a small device the watch nearly fills its window, so the gap is smaller
    than the margin."""
    img = light_device_art_window()
    d = ImageDraw.Draw(img)
    # Real sim chrome: a pale bar whose CONTENT is dark — a window title, a
    # separator rule, toolbar glyphs — with white canvas between it and the
    # watch. The corners stay bright, so the capture still reads as device art.
    d.rectangle([150, 60, 550, 80], fill=(90, 90, 90))        # title text
    d.rectangle([0, 88, 699, 90], fill=(90, 90, 90))          # separator rule
    d.rectangle([0, 812, 699, 814], fill=(90, 90, 90))        # toolbar rule
    d.rectangle([150, 830, 550, 850], fill=(90, 90, 90))      # toolbar glyphs
    y0, y1 = su.detect_device_art_geometry(img)[1::2]
    assert y0 > 90 and y1 <= 812


def test_a_dark_colorway_detects_exactly_as_before():
    """The light pass is a FALLBACK: dark art must still be measured by the
    dark mask, so no existing store image shifts."""
    from ciqkit.capture import framing
    img = device_art_window()
    a = np.asarray(img.convert("RGB"))
    assert su.detect_device_art_geometry(img) == framing._device_art_box(a, 150)


def test_the_detected_device_art_box_is_plain_ints():
    """The geometry travels in a session dict; numpy scalars would leak into
    anything that later serialises it."""
    box = su.detect_device_art_geometry(device_art_window())
    assert all(type(v) is int for v in box)


def test_autocrop_box_matches_the_autocrop_transform():
    """_autocrop_native_to_screen == crop by autocrop_screen_box, so a caller
    can freeze the box once and replay it per frame."""
    pytest.importorskip("cv2")
    # Full-window-ish capture: white chrome corners, big dark screen block.
    img = Image.new("RGB", (600, 600), (245, 245, 245))
    d = ImageDraw.Draw(img)
    d.rectangle([120, 140, 480, 500], fill=(5, 5, 5))
    box = su.autocrop_screen_box(img)
    assert box is not None
    assert (np.asarray(su._autocrop_native_to_screen(img))
            == np.asarray(img.crop(box))).all()


def test_a_screen_only_capture_needs_no_autocrop_box():
    pytest.importorskip("cv2")
    bare = Image.new("RGB", (454, 454), (8, 8, 8))
    assert su.autocrop_screen_box(bare) is None


# --- frame signatures: the "did the screen change" test ----------------------

def test_an_unchanged_screen_reads_as_unchanged():
    img = sim_window()
    sig = su.face_region_signature(img, CX, CY, FACE_R)
    assert su.signatures_differ(sig, sig) is False


def test_a_changed_screen_is_detected():
    before = su.face_region_signature(sim_window(), CX, CY, FACE_R)
    def content(d):
        d.rectangle([CX - 150, CY - 150, CX + 150, CY + 150], fill=(255, 255, 255))
    after = su.face_region_signature(sim_window(content=content), CX, CY, FACE_R)
    assert su.signatures_differ(before, after) is True


def test_a_tiny_change_is_ignored():
    """The threshold exists so a clock digit or an antialiasing wobble does not
    read as a screen transition."""
    before = su.face_region_signature(sim_window(face_fill=(0, 0, 0)), CX, CY, FACE_R)
    after = su.face_region_signature(sim_window(face_fill=(2, 2, 2)), CX, CY, FACE_R)
    assert su.signatures_differ(before, after) is False


def _board(marks=()):
    """A dark face with a thin grey 3x3 grid and blue X marks in some cells --
    sparse content, like a game board."""
    g0, cell = CX - 150, 100

    def content(d):
        for k in (1, 2):
            d.line([g0 + k * cell, g0, g0 + k * cell, g0 + 3 * cell], fill=(150, 150, 150), width=3)
            d.line([g0, g0 + k * cell, g0 + 3 * cell, g0 + k * cell], fill=(150, 150, 150), width=3)
        for i in marks:
            x, y = g0 + (i % 3) * cell, g0 + (i // 3) * cell
            d.line([x + 25, y + 25, x + 75, y + 75], fill=(0, 170, 255), width=10)
            d.line([x + 75, y + 25, x + 25, y + 75], fill=(0, 170, 255), width=10)
    return sim_window(content=content)


def test_one_mark_on_a_sparse_board_is_a_change():
    """The coarse 4x4 average misses it; the detail test must not. Missing it
    made ciqkit retry a START press that had worked: a second move."""
    before, after = _board(marks=(4,)), _board(marks=(4, 0))
    coarse = (su.face_region_signature(before, CX, CY, FACE_R),
              su.face_region_signature(after, CX, CY, FACE_R))
    assert su.signatures_differ(*coarse) is False      # why the detail test exists
    assert su.details_differ(su.frame_detail_signature(before),
                             su.frame_detail_signature(after)) is True


def test_one_mark_on_a_rectangular_screen_is_a_change():
    """Face detection assumes a round display; on a rectangular one it measured
    the device body, and a face-relative region missed a mark near the
    screen's edge. The frame-wide detail test needs no geometry."""
    def window(marks):
        img = Image.new("RGB", (1140, 1310), (235, 235, 235))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([90, 120, 1050, 1200], radius=120, fill=(20, 20, 20))  # body
        d.rectangle([250, 300, 890, 1020], fill=(0, 0, 0))                         # 640x720 screen
        for i in marks:   # ~180 px cells, 18 px strokes, like a 2x Retina capture
            x, y = 300 + (i % 3) * 180, 400 + (i // 3) * 180
            d.ellipse([x + 45, y + 45, x + 135, y + 135], outline=(255, 85, 0), width=18)
        return img
    before, after = window((4,)), window((4, 8))
    assert su.details_differ(su.frame_detail_signature(before),
                             su.frame_detail_signature(after)) is True


def test_details_ignore_noise_and_a_uniform_near_black_shift():
    rng = np.random.default_rng(0)
    clean = _board(marks=(4,))
    noisy = Image.fromarray(np.clip(np.asarray(clean, dtype=np.int16)
                                    + rng.integers(-6, 7, size=(WIN, WIN, 3)),
                                    0, 255).astype(np.uint8))
    sig = su.frame_detail_signature(clean)
    assert su.details_differ(sig, sig) is False
    assert su.details_differ(sig, su.frame_detail_signature(noisy)) is False
    assert su.details_differ(
        su.frame_detail_signature(sim_window(face_fill=(0, 0, 0))),
        su.frame_detail_signature(sim_window(face_fill=(2, 2, 2)))) is False
    assert su.details_differ((1.0, 2.0), (1.0,)) is False


def test_a_strong_local_change_counts_even_when_the_mean_does_not():
    """Measured on a real paged app: main page -> next page moved the title
    band by 37/39 and nothing else much (mean 10.2 < 12). Reading that as "no
    change" retried the swipe and over-navigated two extra pages."""
    before = (0.0,) * 16
    after = (0.6, 36.6, 39.0, 1.2, 7.6, 9.3, 8.6, 7.6,
             6.3, 7.3, 6.3, 6.9, 4.2, 7.9, 10.0, 3.8)
    assert su.signatures_differ(before, after) is True


def test_one_strongly_changed_cell_alone_is_not_a_transition():
    """A single digit ticking over can light up one cell; that must not
    confirm a gesture that never landed."""
    before = (0.0,) * 16
    after = (60.0,) + (2.0,) * 15
    assert su.signatures_differ(before, after) is False


def _text_line(x0, y0, x1, y1, step):
    """Glyph-like vertical strokes: what a line of text looks like at the
    signature's resolution."""
    def draw(d):
        for x in range(x0, x1, step):
            d.rectangle([x, y0, x + 3, y1], fill=(255, 255, 255))
    return draw


def test_text_replaced_in_place_is_a_change():
    """"MINI OK" -> "HELD" on a black screen moved no 4x4 cell by more than
    9.4, so every confirmed tap or hold that only rewrote a label read as
    "the gesture did not land" -- and was retried."""
    before = su.face_region_signature(
        sim_window(content=_text_line(CX - 120, CY - 20, CX + 120, CY + 20, 14)),
        CX, CY, FACE_R)
    after = su.face_region_signature(
        sim_window(content=_text_line(CX - 113, CY - 20, CX + 113, CY + 20, 23)),
        CX, CY, FACE_R)
    assert su.signatures_differ(before, after) is True


def test_one_ticking_digit_is_not_a_change():
    before = su.face_region_signature(
        sim_window(content=_text_line(CX, CY, CX + 30, CY + 40, 12)), CX, CY, FACE_R)
    after = su.face_region_signature(
        sim_window(content=_text_line(CX + 4, CY, CX + 30, CY + 40, 9)), CX, CY, FACE_R)
    assert su.signatures_differ(before, after) is False


def test_signatures_of_different_lengths_never_compare_equal():
    assert su.signatures_differ((1, 2, 3), (1, 2)) is False
    assert su.signatures_differ((), ()) is False


def test_a_blank_screen_does_not_look_like_a_loaded_app():
    assert su._looks_like_app_loaded(Image.new("RGB", (454, 454), (0, 0, 0))) is False


def test_a_screen_with_content_looks_loaded():
    img = Image.new("RGB", (454, 454), (10, 10, 10))
    d = ImageDraw.Draw(img)
    for i in range(8):
        d.rectangle([60, 40 + i * 40, 394, 60 + i * 40], fill=(240, 240, 240))
    assert su._looks_like_app_loaded(img) is True


# --- module boundaries -------------------------------------------------------

def test_the_pure_imaging_half_imports_without_any_gui_plumbing():
    """geometry/framing must not drag in the window, input or SDK machinery.

    That separation is the point of splitting them out of `screenshot_utils`:
    it is what lets these tests run headless in CI on a plain Linux box. If
    something platform-specific creeps back in, importing them stops being
    free and the boundary has quietly gone.
    """
    import ciqkit.capture.framing as framing
    import ciqkit.capture.geometry as geometry

    for mod in (geometry, framing):
        assert not hasattr(mod, "pyautogui")
        assert not hasattr(mod, "ensure_simulator")
        assert not hasattr(mod, "capture_window")
        assert not hasattr(mod, "_sdk")


def test_screenshot_utils_still_exposes_the_names_it_moved_out():
    """The split is a move, not a rename: every caller keeps working."""
    from ciqkit.capture import screenshot_utils as su
    for name in ("find_watch_face_auto", "find_display_circle",
                 "device_art_screen_circle", "crop_face", "crop_watch_body",
                 "face_region_signature", "signatures_differ",
                 "frame_detail_signature", "details_differ",
                 "_looks_like_app_loaded", "make_framed_screenshot",
                 "make_sdk_framed_screenshot", "frame_device_art_capture",
                 "STORE_SCREENSHOT_SIZE", "STORE_BG_COLOR"):
        assert hasattr(su, name), name


def test_sdk_bin_refuses_to_answer_before_init():
    """Unset, the SDK path used to join to "/bin/monkeydo".

    An absolute path to the wrong thing. The caller then got a
    FileNotFoundError from a Popen three frames away -- or, on a machine that
    happens to have that path, launched something else entirely.
    """
    from ciqkit.capture import screenshot_utils as su
    saved = su._sdk
    su._sdk = ""
    try:
        with pytest.raises(su.SimulatorLaunchError, match="init"):
            su.sdk_bin("monkeydo")
    finally:
        su._sdk = saved


def test_a_failed_simulator_launch_raises_instead_of_exiting():
    """`sys.exit` from library code killed the broker's scheduler thread.

    SystemExit is a BaseException, so every sensible `except Exception` guard
    let it through, and inside a thread that is a silent death.
    """
    from ciqkit.capture import screenshot_utils as su
    assert issubclass(su.SimulatorLaunchError, Exception)
    saved_sdk, saved_find = su._sdk, su.find_sim_window
    su._sdk = ""
    su.find_sim_window = lambda: None
    try:
        with pytest.raises(su.SimulatorLaunchError):
            su.ensure_simulator()
    finally:
        su._sdk, su.find_sim_window = saved_sdk, saved_find


def test_a_never_captured_window_handle_warns_before_clicking(caplog):
    """The content offset / retina scale are a capture-time side-channel.

    A handle straight from find_sim_window() still carries the neutral
    defaults, so a click computed from it ignores the chrome the capture would
    have trimmed. Aiming blind is exactly how a "verified" gesture ends up
    verifying the wrong screen, so it must at least say so.
    """
    from ciqkit.capture import screenshot_utils as su
    w = su._MacWindow(100, 200, 400, 400)
    assert w.geometry_captured is False
    with caplog.at_level("WARNING", logger="ciqkit.capture"):
        sx, sy = su._screen_xy(w, 10, 20)
    assert (sx, sy) == (110, 220)
    assert any("never been captured" in r.message for r in caplog.records)


# --- input coordinate mapping ------------------------------------------------

def test_a_tap_targets_the_display_not_the_whole_window():
    """The chrome offset used to be dropped, so taps aimed at the window.

    Fractions are of the DISPLAY CONTENT: with the simulator's title bar
    trimmed off the top of the capture, the middle of the display is NOT the
    middle of the window, and a y=0.5 tap has to account for that.
    """
    from ciqkit.capture import screenshot_utils as su
    w = su._MacWindow(1000, 500, 400, 440)   # 40px of chrome, logical
    # A 2x Retina capture: content is 800x800 image px at (0, 80).
    w.content_left, w.content_top = 0, 80
    w.content_w, w.content_h = 800, 800
    w.retina_scale = 2.0
    w.geometry_captured = True

    cx, cy = su.content_point(w, 0.5, 0.5)
    # Display centre: x = 1000 + (0 + 400)/2, y = 500 + (80 + 400)/2
    assert (cx, cy) == (1200, 740)
    # Whole-window centre would have been y = 500 + 220 = 720: the 20 logical
    # pixels of trimmed chrome are exactly what used to be lost.
    assert cy != 500 + w.height // 2


def test_the_mapping_is_unchanged_when_there_is_no_chrome_and_no_retina():
    """The fix must not move taps on a plain 1x window with a full-bleed capture."""
    from ciqkit.capture import screenshot_utils as su
    w = su._MacWindow(0, 0, 400, 400)
    w.content_left, w.content_top = 0, 0
    w.content_w, w.content_h = 400, 400
    w.retina_scale = 1.0
    w.geometry_captured = True
    for fx, fy in ((0.5, 0.5), (0.2, 0.8), (0.0, 1.0)):
        assert su.content_point(w, fx, fy) == (int(400 * fx), int(400 * fy))


def test_a_span_is_a_fraction_of_the_display_in_logical_pixels():
    from ciqkit.capture import screenshot_utils as su
    w = su._MacWindow(0, 0, 400, 400)
    w.content_w, w.content_h = 800, 800     # 2x capture
    w.retina_scale = 2.0
    w.geometry_captured = True
    assert su.content_span(w, 0.84) == int(800 * 0.84 / 2.0)


def test_geometry_survives_the_handle_being_rebuilt(monkeypatch):
    """SimSession._rebind() rebuilds the handle before EVERY gesture.

    Without carrying the geometry across, the capture's offsets were computed
    and then discarded microseconds later, every single time.
    """
    from ciqkit.capture import screenshot_utils as su
    hot = su._MacWindow(10, 20, 400, 400)
    hot.content_left, hot.content_top = 4, 30
    hot.content_w, hot.content_h = 700, 700
    hot.retina_scale = 2.0
    hot.geometry_captured = True
    su._remember_geometry(hot)

    fresh = su._MacWindow(10, 20, 400, 400)          # same rect
    assert fresh.geometry_captured is False
    su._restore_geometry(fresh)
    assert fresh.geometry_captured is True
    assert (fresh.content_left, fresh.content_top) == (4, 30)
    assert fresh.retina_scale == 2.0

    moved = su._MacWindow(999, 20, 400, 400)         # window was dragged
    su._restore_geometry(moved)
    assert moved.geometry_captured is False, \
        "stale geometry was re-attached to a window that had moved"


def test_aiming_without_a_capture_warns_and_falls_back_to_the_window(caplog):
    from ciqkit.capture import screenshot_utils as su
    w = su._MacWindow(100, 200, 400, 400)
    with caplog.at_level("WARNING", logger="ciqkit.capture"):
        assert su.content_point(w, 0.5, 0.5) == (300, 400)
    assert any("never been captured" in r.message for r in caplog.records)


def test_a_display_box_makes_fractions_land_on_the_screen(monkeypatch):
    """Windows' device panel draws the watch body and band around the
    display, so a fraction of the whole content is a fraction of the DEVICE:
    y=0.85 there is the bezel. With the display located, fractions are of the
    screen the docs promise."""
    from ciqkit.capture import screenshot_utils as su
    monkeypatch.setattr(su, "FRACTIONS_OF_DISPLAY", True)
    w = su._SimWindow(312, 312, 1304, 1840)       # physical px, 200 % display
    w.content_left, w.content_top = 9, 52          # the panel inside the frame
    w.content_w, w.content_h = 634, 836            # image px (0.5 of physical)
    w.retina_scale = 0.5
    w.geometry_captured = True
    whole = su.content_point(w, 0.5, 0.85)
    su.set_display_box(w, (90, 239, 544, 693))    # the 454 px venu3 face
    x, y = su.content_point(w, 0.5, 0.85)
    assert (x, y) == (312 + int((9 + 90 + 454 * 0.5) / 0.5),
                      312 + int((52 + 239 + 454 * 0.85) / 0.5))
    assert y < whole[1], "0.85 of the device is lower than 0.85 of the screen"
    assert su.content_span(w, 0.84) == int(454 * 0.84 / 0.5)
    # Remembered with the capture geometry, so a rebuilt handle keeps it ...
    fresh = su._SimWindow(312, 312, 1304, 1840)
    su._restore_geometry(fresh)
    assert fresh.display_box == (90, 239, 544, 693)
    # ... and macOS (FRACTIONS_OF_DISPLAY off) keeps its measured behaviour.
    monkeypatch.setattr(su, "FRACTIONS_OF_DISPLAY", False)
    assert su.content_point(w, 0.5, 0.85) == whole
