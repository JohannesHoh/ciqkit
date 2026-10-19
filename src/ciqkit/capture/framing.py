"""Store-ready framing: the captured face composited onto a watch body.

Two routes, in order of fidelity:

  * ``make_sdk_framed_screenshot`` -- paste the native-resolution face into the
    transparent screen cut-out of Garmin's own SDK device PNG, which already
    ships the watch body and band. This is the good one.
  * ``make_framed_screenshot`` -- the legacy pixel-walk body crop, used only
    when no SDK device PNG is installed for the device. It mis-crops bright
    pages, because their content trips its "background is white" detector.

``frame_device_art_capture`` handles the third case: the simulator was already
in its device-art display mode, so it drew the body itself and re-framing would
double it.

Pure imaging, like :mod:`ciqkit.capture.geometry` -- no window handles.
"""
from __future__ import annotations

import functools
import logging
import os

import numpy as np
from PIL import Image, ImageChops

from .geometry import (
    MIN_DISPLAY_WIDTH, _corner_brightness, _looks_like_app_loaded,
    find_watch_face_auto,
)

log = logging.getLogger("ciqkit.capture")

# Store screenshot canvas: 1080x1080 on a near-black background.
STORE_SCREENSHOT_SIZE = 1080
STORE_BG_COLOR = (15, 15, 26)   # dark background behind framed store shots

# Device-art watch masks. WATCH_DARK_MAX is "solidly dark" — a black colorway's
# body/band/bezel, and well below the SDK drop shadow (~180-200). A light
# colorway needs the canvas-relative pass instead: the ivory venu2s band is
# ~190, so the only way to separate it from the pure-white canvas is contrast
# against the canvas, not an absolute cut.
WATCH_DARK_MAX = 150
CANVAS_CONTRAST = 25

#: A circle smaller than this is not a watch display — it is a dialog,
#: a toolbar glyph, or a mis-measured body. Mirrors MIN_DISPLAY_WIDTH,
#: which the brightness detector applies to the same question.
MIN_DISPLAY_RADIUS = MIN_DISPLAY_WIDTH // 2


def make_framed_screenshot(watch_crop, bg_color=STORE_BG_COLOR,
                           size=STORE_SCREENSHOT_SIZE):
    """Composite the watch body on a premium dark background at size x size."""
    max_dim = size * 0.88
    scale   = min(max_dim / watch_crop.width, max_dim / watch_crop.height)
    nw, nh  = int(watch_crop.width * scale), int(watch_crop.height * scale)

    rgba = watch_crop.convert("RGBA")
    data = np.array(rgba, dtype=np.uint8).copy()

    thresh_low, thresh_high = 180, 235
    min_chan   = data[:, :, :3].min(axis=2).astype(np.float32)
    near_white = np.all(data[:, :, :3] > thresh_low, axis=2)
    alpha_frac = np.clip(
        (min_chan - thresh_low) / (thresh_high - thresh_low), 0.0, 1.0)
    new_alpha = (255 * (1.0 - alpha_frac)).astype(np.uint8)
    data[near_white, 3] = new_alpha[near_white]

    watch_rgba = Image.fromarray(data, "RGBA").resize((nw, nh), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), (*bg_color, 255))
    x = (size - nw) // 2
    y = (size - nh) // 2
    out.paste(watch_rgba, (x, y), watch_rgba)
    return out.convert("RGB")


# ---------------------------------------------------------------------------
# Reliable framing: composite the native-res face onto Garmin's official SDK
# device PNG (which already ships watch body + band as an alpha-cut overlay).
# Eliminates the previous pixel-walking body crop, which mis-cropped idle
# pages because their content tripped the bright-pixel "bg" detector.
# ---------------------------------------------------------------------------

_DARK = 200   # "part of the watch" in a device image: any channel below this


def _dark_bbox(arr):
    """(x0, y0, x1, y1) of the pixels whose darkest channel is below _DARK,
    ignoring rows/columns with only a stray pixel or two. None if nothing."""
    m = arr.min(axis=2) < _DARK
    cols = np.where(m.sum(axis=0) > 2)[0]
    rows = np.where(m.sum(axis=1) > 2)[0]
    if cols.size == 0 or rows.size == 0:
        return None
    return int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1


@functools.lru_cache(maxsize=8)
def _sim_device_layout(sim_json, _mtime_ns):
    """(dark bbox of the device image, display rect on it) from a device's
    simulator.json, or None. Cached: read on every gesture and every poll."""
    import json
    try:
        with open(sim_json, encoding="utf-8") as fh:
            d = json.load(fh)
        loc = d["display"]["location"]
        png = os.path.join(os.path.dirname(sim_json), d["image"])
        arr = np.array(Image.open(png).convert("RGB"))
    except (OSError, KeyError, TypeError, ValueError):
        return None
    bbox = _dark_bbox(arr)
    if bbox is None:
        return None
    return bbox, (int(loc["x"]), int(loc["y"]), int(loc["width"]), int(loc["height"]))


def _sim_json_path(device_id):
    from ..sim.preflight import devices_root
    root = devices_root()
    if not root or not device_id:
        return None
    return os.path.join(root, device_id, "simulator.json")


def sim_device_keys(device_id, sim_json=None):
    """The device's physical buttons from its simulator.json ``keys``:
    ``[{"id", "behavior", "hold", "rect": (x, y, w, h)}]`` with rects in
    device-image pixels, or [] when the profile lists none (or is missing)."""
    import json
    path = sim_json or _sim_json_path(device_id)
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            keys = json.load(fh).get("keys") or []
    except (OSError, ValueError, AttributeError):
        return []
    out = []
    for k in keys:
        try:
            loc = k["location"]
            out.append({"id": str(k["id"]), "behavior": k.get("behavior"),
                        "hold": bool(k.get("isHold", False)),
                        "rect": (int(loc["x"]), int(loc["y"]),
                                 int(loc["width"]), int(loc["height"]))})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _device_mask(sim_json):
    """(watch, valid) boolean masks of a device's own PNG, or None.

    ``watch`` is "part of the watch" (opaque and dark). ``valid`` leaves out
    the display cut-out: the PNG is transparent there but the simulator fills
    it with whatever the app draws, so it says nothing about placement.
    """
    import json
    try:
        with open(sim_json, encoding="utf-8") as fh:
            meta = json.load(fh)
        png = os.path.join(os.path.dirname(sim_json), meta["image"])
        loc = meta["display"]["location"]
        a = np.asarray(Image.open(png).convert("RGBA"))
    except (OSError, KeyError, TypeError, ValueError):
        return None
    watch = (a[:, :, :3].min(axis=2) < _DARK) & (a[:, :, 3] > 128)
    valid = np.ones(watch.shape, bool)
    x, y, w, h = (int(loc[k]) for k in ("x", "y", "width", "height"))
    valid[max(0, y):y + h, max(0, x):x + w] = False
    return watch, valid


def _match_offset(cap, dev, valid, step, around=None, radius=None):
    """(score, dx, dy): where mask ``dev`` best sits inside mask ``cap``
    (offsets of dev's top-left in cap), by agreement over dev's own area.
    Searches every offset at ``step`` px, or +-radius around ``around``."""
    h, w = dev.shape
    H, W = cap.shape
    n = float(valid.sum()) or 1.0
    if h > H or w > W:
        return None
    if around is None:
        xs, ys = range(0, W - w + 1, step), range(0, H - h + 1, step)
    else:
        ax, ay = around
        xs = range(max(0, ax - radius), min(W - w, ax + radius) + 1)
        ys = range(max(0, ay - radius), min(H - h, ay + radius) + 1)
    best = None
    for dy in ys:
        for dx in xs:
            sc = float(((cap[dy:dy + h, dx:dx + w] == dev) & valid).sum()) / n
            if best is None or sc > best[0]:
                best = (sc, dx, dy)
    return best


def sim_device_point(img, device_id, px, py, sim_json=None, scale=None):
    """Map a point in DEVICE-IMAGE pixels (the coordinates simulator.json
    uses) to pixels of ``img``, a capture of the simulator window.

    The simulator draws the device PNG 1:1 in logical pixels, so the capture
    holds it at the Retina scale (``scale``; 1 and 2 are tried when omitted)
    somewhere inside the window chrome. The offset is found by sliding the
    PNG's own silhouette over the capture's and keeping the best agreement --
    chrome-proof (title bar, border and toolbar only cost every candidate the
    same few pixels) and content-proof (the watch body is far larger than any
    screen content). Returns (x, y) floats, or None when no placement
    agrees well enough: not this device's art, or a differently drawn view.
    """
    path = sim_json or _sim_json_path(device_id)
    if not path:
        return None
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return None
    masks = _device_mask_cached(path, mtime)
    if masks is None:
        return None
    dev, valid = masks
    cap_full = np.asarray(img.convert("RGB")).min(axis=2) < _DARK
    best = None
    for sc in ([float(scale)] if scale else [1.0, 2.0]):
        if sc <= 0:
            continue
        size = (max(1, round(dev.shape[1] * sc)), max(1, round(dev.shape[0] * sc)))

        def scaled(m, size=size):
            return np.asarray(Image.fromarray(m.astype(np.uint8) * 255).resize(
                size, Image.NEAREST)) > 127
        d, v = scaled(dev), scaled(valid)
        # coarse (every 4th pixel of both), then exact within +-4
        coarse = _match_offset(cap_full[::4, ::4], d[::4, ::4], v[::4, ::4], 1)
        if coarse is None:
            continue
        fine = _match_offset(cap_full, d, v, 1, (coarse[1] * 4, coarse[2] * 4), 4)
        if fine and (best is None or fine[0] > best[0]):
            best = (fine[0], fine[1], fine[2], sc)
    if best is None or best[0] < 0.9:
        return None
    _score, ox, oy, sc = best
    return ox + px * sc, oy + py * sc


@functools.lru_cache(maxsize=8)
def _device_mask_cached(sim_json, _mtime_ns):
    return _device_mask(sim_json)


def sim_button_point(img, device_id, key_id, sim_json=None, scale=None):
    """Centre of the button ``key_id`` inside capture ``img``, in image pixels.

    Raises ValueError listing the available ids when the device has no such
    key (or none at all). Returns ``(x, y, key)`` or None when the capture
    cannot be aligned to the device art.
    """
    keys = sim_device_keys(device_id, sim_json)
    if not keys:
        raise ValueError(
            f"device {device_id!r} lists no physical keys in its "
            f"simulator.json, so a `button` step cannot be aimed; use "
            f"tap/swipe, or `key` for keyboard-driven input")
    match = [k for k in keys if k["id"] == key_id]
    if not match:
        have = ", ".join(sorted({k["id"] for k in keys}))
        raise ValueError(f"device {device_id!r} has no button {key_id!r}; "
                         f"available: {have}")
    key = match[0]
    x, y, w, h = key["rect"]
    pt = sim_device_point(img, device_id, x + w / 2.0, y + h / 2.0, sim_json,
                          scale)
    if pt is None:
        return None
    return pt[0], pt[1], key


def sim_display_box(img, device_id):
    """The watch display's box (x0, y0, x1, y1) inside a capture of the
    simulator's device-art view, or None.

    Not detected from pixels -- LOOKED UP. The device profile's
    simulator.json says exactly where the display sits on the device image
    (venu3: 454x454 at (91, 206) of a 636x882 PNG), and where the simulator
    draws that image 1:1 -- the Windows simulator always does -- aligning the
    image's dark silhouette with the captured one gives the offset. The top
    and both sides of the silhouette are always in view (the window may cut
    the strap off at the bottom), so they are what is aligned.

    This replaces brightness-based face detection where it can, which was
    measured ~110 px off on a bright Windows screen -- enough to put every
    fractional tap on the wrong control. It refuses (None) unless the
    captured silhouette is the device image's own width to within a few
    pixels, so a scaled (Retina) or differently drawn capture falls back to
    the detectors instead of getting a confidently wrong box.
    """
    from ..sim.preflight import devices_root
    root = devices_root()
    if not root or not device_id:
        return None
    sim_json = os.path.join(root, device_id, "simulator.json")
    try:
        layout = _sim_device_layout(sim_json, os.stat(sim_json).st_mtime_ns)
    except OSError:
        return None
    if layout is None:
        return None
    (px0, py0, px1, _py1), (dx, dy, dw, dh) = layout
    cap = _dark_bbox(np.asarray(img.convert("RGB")))
    if cap is None:
        return None
    cx0, cy0, cx1, _cy1 = cap
    if abs((cx1 - cx0) - (px1 - px0)) > 3:
        return None                       # not this device image at 1:1
    ox, oy = cx0 - px0, cy0 - py0
    box = (ox + dx, oy + dy, ox + dx + dw, oy + dy + dh)
    w, h = img.size
    if box[0] < 0 or box[1] < 0 or box[2] > w or box[3] > h:
        return None
    return box


def _sdk_device_png_path(device_id):
    """Return the Garmin SDK device PNG path for `device_id`, or None."""
    from ..sim.preflight import devices_root   # stdlib-only, no cycle
    home = devices_root()
    if not home:
        return None
    p = os.path.join(home, device_id, f"{device_id}.png")
    return p if os.path.exists(p) else None


def _detect_sdk_screen_rect(device_png):
    """Find the transparent screen rectangle (alpha == 0) in the SDK device
    PNG. Returns (left, top, right, bottom)."""
    arr = np.array(device_png)
    a = arr[:, :, 3]
    ys, xs = np.where(a == 0)
    if len(xs) == 0:
        raise RuntimeError("SDK device PNG has no transparent screen region")
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _autocrop_native_to_screen(face_img):
    """Crop the sim-native PNG down to just the dark round screen content.

    Some capture paths write a wide crop that contains the body bezel,
    crown, and white sim-window margin in addition to the actual screen
    pixels. Pasting that into the SDK PNG's screen rectangle leaves bezel
    detail and white corners visible OUTSIDE the round screen circle on
    the final composite. Detect the largest dark region (the screen) and
    crop to it so only screen pixels reach the framer.

    Falls back to the input image untouched if cv2 isn't available or no
    dark region is found.
    """
    box = autocrop_screen_box(face_img)
    return face_img.crop(box) if box else face_img


def autocrop_screen_box(face_img):
    """The crop box :func:`_autocrop_native_to_screen` would apply, or None.

    Split out so a caller that must apply the SAME transform to many frames
    (an animation recording) can detect the box once and reuse it. The
    detection is content-dependent -- the dark-region bounding box moves with
    what the screen happens to show -- so re-running it per frame makes the
    clip's framing wobble.
    """
    try:
        import cv2 as _cv2
    except ImportError:
        return None
    arr = np.asarray(face_img.convert("RGB"))
    H, W = arr.shape[:2]
    # Only FULL-WINDOW sim captures (body + bezel + WHITE sim-window chrome)
    # need autocropping. Detect that chrome by sampling the four corners: a
    # full-window capture has near-white sim-window margin there, while a
    # A screen-only capture has the DARK black bezel ring just outside the
    # round glass. Gating on a dark-fraction threshold instead mis-fired on
    # dark-heavy pages: their mostly-black content
    # tripped the 20 % gate, so the connected-component crop below grabbed an
    # offset sub-region and the page rendered shrunk + clipped. Corner
    # brightness is the reliable discriminator.
    cs = max(4, min(H, W) // 40)
    corner_means = [
        float(arr[0:cs, 0:cs].mean()),
        float(arr[0:cs, W - cs:W].mean()),
        float(arr[H - cs:H, 0:cs].mean()),
        float(arr[H - cs:H, W - cs:W].mean()),
    ]
    if min(corner_means) < 200:
        # Corners aren't white sim-window chrome → screen-only capture. The
        # whole frame IS the screen; leave it untouched.
        return None
    dark = (arr.min(axis=2) < 40).astype(np.uint8)
    if dark.sum() < (H * W * 0.20):
        # Less than 20 % dark → almost certainly the whole frame IS the
        # screen (screen-only capture). Leave alone.
        return None
    n_lbl, lbl, stats, _ = _cv2.connectedComponentsWithStats(dark, connectivity=8)
    if n_lbl <= 1:
        return None
    biggest = 1 + int(np.argmax(stats[1:, _cv2.CC_STAT_AREA]))
    area = int(stats[biggest, _cv2.CC_STAT_AREA])
    if area < (H * W * 0.15):
        return None
    x = int(stats[biggest, _cv2.CC_STAT_LEFT])
    y = int(stats[biggest, _cv2.CC_STAT_TOP])
    w = int(stats[biggest, _cv2.CC_STAT_WIDTH])
    h = int(stats[biggest, _cv2.CC_STAT_HEIGHT])
    # If the detected dark region already fills ~the whole frame the
    # native is screen-only — skip the crop to keep AA edges intact.
    if w >= W * 0.92 and h >= H * 0.92:
        return None
    # Square the crop to keep aspect ratio for the round screen mask.
    side = max(w, h)
    cx = x + w // 2
    cy = y + h // 2
    half = side // 2
    left = max(0, cx - half)
    top = max(0, cy - half)
    right = min(W, left + side)
    bottom = min(H, top + side)
    # Re-pull left/top in case clamp shifted the box (keeps the box square).
    if right - left < side:
        left = max(0, right - side)
    if bottom - top < side:
        top = max(0, bottom - side)
    return (left, top, right, bottom)


def frame_device_art_capture(img, bg_color=STORE_BG_COLOR,
                             size=STORE_SCREENSHOT_SIZE):
    """Store-frame a capture in which the SIMULATOR itself already rendered the
    watch body + silicone band (its "device art" display mode) on a white
    canvas. Returns a size x size render of that watch centred on the premium
    dark background, or None when the capture is NOT device art (a bare display
    on chrome) so the caller falls back to make_sdk_framed_screenshot.

    Why this exists: on device-art captures the normal pipeline (detect the
    round DISPLAY, re-composite it onto an SDK body PNG) DOUBLE-frames — it
    pastes the sim's own watch body inside a second body, and on venu3 the
    near-black bezel merges with the black display so the display radius can't
    be found reliably; a bright screen over-detects, and the store shot then
    shows a ghost ring around shrunken content. The
    sim's device art IS already a store-quality watch render, so here we simply
    trim it off the white canvas and drop it on the dark background — keying on
    the whole dark-on-white watch, which is robust regardless of screen content.

    Split into :func:`detect_device_art_geometry` (find the watch's crop box)
    and :func:`apply_device_art_frame` (crop + background swap + resize) so an
    animation recording can detect ONCE and apply the identical transform to
    every frame -- the detection reads the frame's content, so re-running it
    per frame lets the crop wobble with what the screen shows.
    """
    box = detect_device_art_geometry(img)
    if box is None:
        return None
    return apply_device_art_frame(img, box, bg_color=bg_color, size=size)


def detect_device_art_geometry(img):
    """The crop box (x0, y0, x1, y1) of the sim-rendered watch (body + band)
    inside a device-art window capture, or None when the capture is not device
    art.

    Detection is content-independent: requires a bright white canvas (device
    art always sits on one) and a watch blob that is clearly TALLER than wide
    (the strap), which a bare round display is not.

    The watch mask is tried at two thresholds, dark-first. Pass 1 keys on
    genuinely dark pixels, which is what a black/slate colorway is, and which
    conveniently excludes the faint SDK drop shadow. Pass 1 is BLIND to a LIGHT
    colorway: venu2s ships ivory, whose body, bezel and silicone band all sit
    around 185-200, so only the black glass registers, the blob comes out
    round, the strap test rejects it, and the caller then re-frames a
    device-art capture onto an SDK body — the double-frame this function
    exists to prevent (gold body, ghost bezel ring with a second set of tick
    marks, shrunken off-centre content). Pass 2 therefore keys on "anything
    meaningfully darker than the canvas", which sees a cream band. Dark-first
    ordering means every colorway pass 1 already handled is untouched.
    """
    a = np.array(img.convert("RGB"))
    corner = _corner_brightness(a.astype(np.float32).mean(2))
    if corner < 180:
        return None  # no white canvas -> not device art

    thresholds = [WATCH_DARK_MAX]
    light = int(corner) - CANVAS_CONTRAST
    if light > WATCH_DARK_MAX:
        thresholds.append(light)
    for thr in thresholds:
        box = _device_art_box(a, thr)
        if box is not None:
            return box
    return None


def _watch_row_span(watch):
    """Vertical extent of the watch in a device-art mask: the largest run of
    rows containing watch pixels that does NOT touch the top/bottom image edge
    — that drops the macOS title bar and the sim's bottom status toolbar (both
    flush to an edge, both separated from the watch by a white-canvas gap).
    Returns (y0, y1) or None."""
    h = watch.shape[0]
    rows = watch.mean(1) > 0.05
    runs, s = [], None
    for i, v in enumerate(rows):
        if v and s is None:
            s = i
        elif not v and s is not None:
            runs.append((s, i))
            s = None
    if s is not None:
        runs.append((s, len(rows)))
    watch_runs = [r for r in runs if r[0] > 2 and r[1] < h - 2]
    if not watch_runs:
        return None
    return max(watch_runs, key=lambda r: r[1] - r[0])


def _device_art_box(a, thr):
    """One detection pass over the RGB array `a`, treating every pixel whose
    darkest channel is below `thr` as watch. Returns the crop box or None."""
    h, w = a.shape[:2]
    watch = a.min(2) < thr

    span = _watch_row_span(watch)
    if span is None:
        return None
    y0, y1 = span

    # Horizontal extent, measured SYMMETRICALLY about the body centre. The watch
    # body is left-right symmetric, but the sim window can carry a one-sided
    # dark divider a little past the body in the white canvas (it left a faint
    # vertical line down the framed store shot). Take the body centre = median x
    # of the widest (equator) dark row, and the half-width = the body's own
    # radius there, so a one-sided artifact falls OUTSIDE the symmetric crop.
    band = watch[y0:y1]
    row_runs = []
    for ry in range(band.shape[0]):
        xr = np.where(band[ry])[0]
        if xr.size < 20:
            continue
        d = np.diff(xr)
        brk = np.where(d > 8)[0]
        seg = max(np.split(xr, brk + 1), key=len)
        row_runs.append((seg[-1] - seg[0], (seg[0] + seg[-1]) // 2))
    if not row_runs:
        return None
    eq_w, eq_cx = max(row_runs, key=lambda r: r[0])
    # Horizontal: a small SYMMETRIC extension of the body radius (4 %) for air.
    # No additive one-sided margin — the sim window can carry a light-grey
    # (~160, so neither "watch" nor floodable "white") divider ~40 px past the
    # body, and an additive margin reaches it, leaving a grey vertical line.
    half = int((eq_w // 2) * 1.04)
    x0, x1 = eq_cx - half, eq_cx + half
    bw, bh = x1 - x0, y1 - y0
    if bw <= 0 or bh <= 0 or bh < bw * 1.15:
        return None  # round (no strap) -> a bare display, not device art

    # Vertical margin only (breathing room above/below the strap), and it stops
    # at the sim's own chrome. A blind margin walks straight back into the
    # title bar and the bottom toolbar — the rows the edge filter above just
    # dropped — and strands their separator rules and toolbar glyphs across the
    # top and bottom of the store shot; on a small device like venu2s the watch
    # nearly fills its window, so that gap is thinner than the margin.
    #
    # "Stop at the nearest other row run" would be wrong: bright screen content
    # SPLITS the watch into several runs, and the margin has to be free to walk
    # up through the watch's own upper half until it reaches canvas. What marks
    # chrome is a canvas GAP first: walk outward, and once canvas has been seen,
    # the next non-canvas row is no longer this watch.
    canvas = (a.min(2) >= thr).mean(1) > 0.98
    vm = max(12, int(0.03 * bh))
    cx0, cy0 = max(0, x0), _margin(canvas, y0, -1, vm, h)
    cx1, cy1 = min(w, x1), _margin(canvas, y1 - 1, 1, vm, h) + 1
    return (int(cx0), int(cy0), int(cx1), int(cy1))


def _margin(canvas, edge, step, vm, h):
    """Walk `vm` rows outward from `edge` in direction `step`, stopping at the
    first non-canvas row that follows a canvas one — the sim chrome across the
    white gap. Returns the row to crop at (inclusive)."""
    out, seen_canvas = edge, False
    for y in range(edge + step, edge + step * (vm + 1), step):
        if not 0 <= y < h:
            break
        if canvas[y]:
            seen_canvas = True
        elif seen_canvas:
            break
        out = y
    return out


def _flood_canvas(crop, bg_color):
    """Paint every border-connected white pixel of a device-art crop with
    ``bg_color``, in place. Seeds along ALL FOUR EDGES (not just the corners):
    the round body curves away from the corners, so a one-sided canvas strip
    beside the body isn't corner-connected and a corner-only flood leaves it as
    a bright vertical line. In-display whites ("Press START", the version
    string) don't touch the border, so they stay."""
    from PIL import ImageDraw
    cw, ch = crop.width, crop.height
    seeds = []
    step = 40
    for x in range(0, cw, step):
        seeds.append((x, 0))
        seeds.append((x, ch - 1))
    for y in range(0, ch, step):
        seeds.append((0, y))
        seeds.append((cw - 1, y))
    for sx, sy in seeds:
        if min(crop.getpixel((sx, sy))) > 195:
            ImageDraw.floodfill(crop, (sx, sy), bg_color, thresh=40)


def device_art_canvas_mask(img, box, bg_color=STORE_BG_COLOR):
    """Which pixels of the ``box`` crop are the simulator's white canvas.

    Returns an "L" mask, white where :func:`apply_device_art_frame` would
    repaint. The flood fill behind it is ~0.47 s on a Retina grab and is by
    far the most expensive step of framing a frame -- but the region it finds
    is the static canvas OUTSIDE the bezel, so for a clip it is identical on
    every frame. :meth:`SimSession._frame_geometry` computes this once and
    hands it back through ``apply_device_art_frame(canvas_mask=...)``, which
    turns 0.47 s of scanline flooding per frame into a paste.

    Derived by diffing the flood against the original: canvas pixels are white
    by definition (the seed test requires min > 195) and ``bg_color`` is dark,
    so every repainted pixel necessarily differs and none is missed. The diff
    is reduced with a per-pixel MAX over the channels, not ``convert("L")``:
    the luminance conversion rounds a small diff to 0, which would silently
    punch holes in the mask if ``bg_color`` were ever changed to something
    close to the canvas white.
    """
    before = img.convert("RGB").crop(box)
    after = before.copy()
    _flood_canvas(after, bg_color)
    diff = np.asarray(ImageChops.difference(before, after)).max(axis=2)
    return Image.fromarray(np.where(diff > 0, 255, 0).astype(np.uint8), "L")


def _canvas_mask_still_fits(crop, canvas_mask, sample_step=32):
    """Does ``canvas_mask`` still describe ``crop``?

    A size check alone proves nothing: PIL pads an out-of-bounds crop, so a
    crop taken after the simulator window moved or was resized is still exactly
    box-sized while describing completely different pixels -- and a mask
    applied to it would repaint the watch instead of the canvas.

    So check the content, not the dimensions: every pixel the mask claims is
    canvas was near-white when the mask was built (the flood only reaches
    pixels within its threshold of a > 195 seed). Sample a sparse grid -- a few
    hundred points, microseconds -- and reject the mask if the claimed canvas
    has gone dark. Sparse is deliberate: this runs per frame, and a window that
    moved invalidates the whole region at once, not one pixel of it. The 95 %
    agreement bar is there for the same reason: it absorbs a few sampled points
    landing on the mask's anti-aliased rim, while any real move darkens far
    more of the region than that.

    A rejection is not a repair. If the window moved, the flood fallback frame
    is wrong too -- the crop itself is misaligned. What this buys is that a
    stale mask never actively repaints the WATCH with background, which is the
    one failure mode that reads as a rendering bug in the app under test.
    """
    if canvas_mask.size != crop.size:
        return False
    m = np.asarray(canvas_mask)[::sample_step, ::sample_step]
    if not m.any():
        return True          # nothing claimed, nothing to contradict
    c = np.asarray(crop.convert("RGB"))[::sample_step, ::sample_step].min(axis=2)
    claimed = c[m > 0]
    # A seed needs min > 195, i.e. min >= 196, and PIL's floodfill compares
    # each candidate against the SEED's value (no cascade) with an L1 threshold
    # of 40 -- so the darkest min-channel the flood can reach is 196 - 40 = 156,
    # and `> 155` is the tight exclusive bound.
    return bool((claimed > 155).mean() >= 0.95)


def apply_device_art_frame(img, box, bg_color=STORE_BG_COLOR,
                           size=STORE_SCREENSHOT_SIZE, canvas_mask=None):
    """Apply a device-art framing transform: crop the watch out at ``box``
    (from :func:`detect_device_art_geometry`), swap the white sim canvas for
    the dark store background, and centre on a size x size square.

    Deterministic in its geometry: the crop, padding and resize depend only on
    ``box``, never on the frame's content, so applying one detected box to
    every frame of a clip yields pixel-stable framing. (The border flood fill
    does read pixels, but only white canvas outside the dark bezel is
    reachable from the border, and that canvas is static across frames.)

    ``canvas_mask`` is that static region, pre-computed by
    :func:`device_art_canvas_mask`; pass it to skip the flood fill entirely.
    It is checked before it is trusted (:func:`_canvas_mask_still_fits`), and a
    mask that no longer describes this frame is dropped in favour of the slow
    flood -- which is not a repair, only a refusal to repaint the watch with
    background. See that function.
    """
    crop = img.convert("RGB").crop(box).copy()

    if canvas_mask is None:
        _flood_canvas(crop, bg_color)
    elif _canvas_mask_still_fits(crop, canvas_mask):
        crop.paste(bg_color, (0, 0), canvas_mask)
    else:
        # Say so. A rejected mask silently costs ~0.47 s a frame, and a mask
        # that is rejected EVERY frame (a window nudged a few px, a dialog
        # parked on the canvas) drops a recording from ~4.6 fps back to ~1.4
        # with no symptom but slowness. Warn once per mask, not per frame.
        if not getattr(canvas_mask, "_ciqkit_rejected", False):
            canvas_mask._ciqkit_rejected = True
            log.warning("device-art canvas mask no longer matches the frame "
                        "(the simulator window probably moved); falling back "
                        "to the per-frame flood fill, which is ~20x slower")
        _flood_canvas(crop, bg_color)

    side = int(max(crop.size) * 1.12)
    canvas = Image.new("RGB", (side, side), bg_color)
    canvas.paste(crop, ((side - crop.width) // 2, (side - crop.height) // 2))
    return canvas.resize((size, size), Image.LANCZOS)


def _sdk_workspace_mask(arr):
    """Which pixels of an SDK device PNG array are the SOLID-WHITE workspace
    surround rather than the watch body.

    Garmin ships the body on an opaque white workspace, so "opaque" alone does
    not mean "watch". Seeding a flood from each corner through near-white
    opaque pixels marks only the surround; white PRINTED on the body (button
    labels, dial markers) is not corner-connected and survives.
    """
    from PIL import ImageDraw
    near_white_opaque = (arr[:, :, :3].min(axis=2) >= 235) & (arr[:, :, 3] > 0)
    scratch_arr = np.where(near_white_opaque[..., None], 255, 0).astype(np.uint8)
    scratch_arr = np.repeat(scratch_arr, 3, axis=2)
    scratch = Image.fromarray(scratch_arr, "RGB")
    for seed in ((0, 0), (scratch.width - 1, 0),
                 (0, scratch.height - 1),
                 (scratch.width - 1, scratch.height - 1)):
        if scratch.getpixel(seed) == (255, 255, 255):
            ImageDraw.floodfill(scratch, seed, (128, 128, 128))
    return np.array(scratch)[:, :, 0] == 128


@functools.lru_cache(maxsize=8)
def _prepared_device_png(device_png_path, _mtime_ns):
    """Garmin's device PNG made ready to composite onto: workspace, halo and
    label specks stripped, plus its transparent screen rect.

    Depends on nothing but the file, and costs ~0.3 s -- most of it a pure-
    Python flood fill -- so it is computed once per device and file version
    instead of once per frame. Re-done for every frame of a clip, this was
    the whole per-frame budget of an SDK-framed recording (~2.8 fps on
    Windows, whose simulator window never shows the full device art and so
    always takes this path). Callers must not modify the returned image.
    """
    device_png = Image.open(device_png_path).convert("RGBA")

    # Garmin SDK PNGs ship the body on a SOLID WHITE workspace and the body
    # edges are anti-aliased *against that white*. Naively zeroing every
    # near-white opaque pixel also nukes legitimate white printing on the
    # watch body (button labels, dial markers). Use PIL ImageDraw.floodfill
    # from each corner through the workspace whitespace, marking it
    # transparent — that strips only the workspace surround, leaving
    # inside-body whites intact.
    # Two cleanups on the SDK PNG before we composite:
    #   1. Strip the SOLID-white workspace surround via a corner-seeded flood
    #      fill through near-white opaque pixels. Inside-body whites stay.
    #   2. Zero the alpha of the BODY-EDGE antialiased halo — semi-transparent
    #      near-white pixels along the body silhouette. Left alone, those
    #      blend with the dark store background and show as a bright gray
    #      glow around the watch.
    arr = np.array(device_png)
    rgb_min_all = arr[:, :, :3].min(axis=2)

    # (1) Flood-fill workspace strip.
    outside = _sdk_workspace_mask(arr)
    arr2 = arr.copy()
    arr2[outside, 3] = 0

    # (2) Halo strip: semi-transparent (alpha < 250) bright (rgb_min >= 200)
    # pixels that aren't already flagged as workspace get fully transparented.
    halo = (arr2[:, :, 3] > 0) & (arr2[:, :, 3] < 250) & (rgb_min_all >= 200)
    arr2[halo, 3] = 0

    # (3) Inner-body specks: pure-white opaque pixels INSIDE the body (button
    # labels printed in white on the SDK PNG at the 9 / 3 o'clock crown +
    # button positions). These survive the corner flood because they aren't
    # corner-connected. Drop any connected pure-white-opaque region whose
    # area is small enough to be a label (< 4000 px on 1080-class device
    # PNGs). The body itself is far larger so it never qualifies. Implemented
    # iteratively via PIL.ImageDraw.floodfill on an RGB scratch since scipy
    # isn't available in the screenshot venv.
    # SDK PNGs print button labels / dial markers as off-white opaque specks
    # at the 9 / 3 o'clock body positions; their RGB sits around (214, 214,
    # 215), not pure 255. The flood-fill from corners can't reach them
    # because they aren't connected to the workspace. Use OpenCV's
    # connectedComponentsWithStats to kill every small connected island of
    # bright-opaque pixels INSIDE the body. The actual body bezel is one
    # huge component and survives the size threshold.
    try:
        import cv2 as _cv2
    except ImportError:
        _cv2 = None
    if _cv2 is not None:
        # Threshold 195 catches the slightly-off-white speckle that 235
        # missed (button-label glyphs render at ~(214, 214, 215) on the
        # venu3 SDK PNG). The body bezel itself is dark (RGB < 100) so it
        # never enters the mask. The size filter below further protects any
        # legitimate component that should survive.
        # Include semi-opaque (alpha >= 100) too — some SDK PNG specks render
        # at alpha ~ 240 with bright RGB and otherwise leak through.
        rgb_min_after = arr2[:, :, :3].min(axis=2)
        bright_opaque = ((rgb_min_after >= 195) & (arr2[:, :, 3] >= 100)).astype(np.uint8)
        if bright_opaque.any():
            n_labels, labels, stats, _ = _cv2.connectedComponentsWithStats(
                bright_opaque, connectivity=8)
            # Stats columns: LEFT, TOP, WIDTH, HEIGHT, AREA. Skip label 0
            # (= background). Body bezel survives because its AREA is huge.
            SMALL_REGION_MAX = 20000  # px — tuned for ~1080-class device PNGs
            kill_labels = []
            for lbl in range(1, n_labels):
                if stats[lbl, _cv2.CC_STAT_AREA] < SMALL_REGION_MAX:
                    kill_labels.append(lbl)
            if kill_labels:
                kill_mask = np.isin(labels, kill_labels)
                arr2[kill_mask, 3] = 0

    device_png_clean = Image.fromarray(arr2, "RGBA")

    return device_png.size, device_png_clean, _detect_sdk_screen_rect(device_png)


def make_sdk_framed_screenshot(face_img, device_id,
                               bg_color=STORE_BG_COLOR,
                               size=STORE_SCREENSHOT_SIZE,
                               autocrop=True):
    """Composite the captured native-res face onto Garmin's SDK device PNG
    (body + band alpha-cut), then centre-paste the result on a premium dark
    background at size x size. Reliable for any device that ships an SDK
    device PNG with a transparent screen area.

    Falls back to the legacy pixel-walking `make_framed_screenshot` if the
    SDK PNG isn't found.

    ``autocrop=False`` skips the content-dependent
    :func:`_autocrop_native_to_screen` pass; a caller framing many frames of a
    clip pre-applies one frozen :func:`autocrop_screen_box` instead, so the
    composite geometry cannot wobble with the screen's content.
    """
    device_png_path = _sdk_device_png_path(device_id)
    if device_png_path is None:
        return None  # caller falls back to legacy framer
    from PIL import ImageDraw
    device_size, device_png_clean, (sx0, sy0, sx1, sy1) = _prepared_device_png(
        device_png_path, os.stat(device_png_path).st_mtime_ns)
    screen_w = sx1 - sx0
    screen_h = sy1 - sy0

    # Some sim captures write the FULL watch view --
    # body, bezel, crown, sim window chrome — into the "native" PNG instead
    # of just the screen content. Detect the dark round screen region inside
    # the native PNG and crop to it so what gets pasted into the SDK PNG's
    # screen rectangle is actually screen pixels only. Falls back to the
    # original face_img when the dark region can't be confidently detected.
    if autocrop:
        face_img = _autocrop_native_to_screen(face_img)
    face_rgba = face_img.convert("RGBA").resize((screen_w, screen_h),
                                                Image.LANCZOS)
    # The native sim capture is a SQUARE PNG with WHITE in the corners
    # outside the round watch face. Pasting that straight into the SDK
    # PNG's transparent screen rectangle leaves four white squares
    # visible at the corners between the round screen and the body
    # bezel. Mask the corners with a round alpha matte BEFORE paste so
    # the SDK body shows through there.
    # Round display: the screen rect is square-ish. Tolerate a few px of
    # detection wobble (exact == broke when the rect came back 454x453, which
    # SKIPPED the mask and left four white squares at the diagonal screen
    # corners). Inset the ellipse 2px to also clip the anti-aliased edge ring.
    if abs(screen_w - screen_h) <= max(screen_w, screen_h) // 10:
        mask = Image.new("L", (screen_w, screen_h), 0)
        ImageDraw.Draw(mask).ellipse((2, 2, screen_w - 3, screen_h - 3),
                                     fill=255)
        face_rgba.putalpha(mask)

    # Paste face UNDER the cleaned device overlay.
    composed = Image.new("RGBA", device_size, (0, 0, 0, 0))
    composed.paste(face_rgba, (sx0, sy0))
    composed.alpha_composite(device_png_clean)

    # Crop composite to its non-zero-alpha bbox so the dark store background
    # shows around the watch instead of around the SDK PNG's full canvas.
    a_arr = np.array(composed)[:, :, 3]
    ys, xs = np.where(a_arr > 0)
    if len(xs) == 0:
        return None
    composed = composed.crop((int(xs.min()), int(ys.min()),
                              int(xs.max()) + 1, int(ys.max()) + 1))

    # Scale composed watch body to fit `size` with ~10% margin.
    body_w, body_h = composed.size
    max_dim = int(size * 0.88)
    scale = min(max_dim / body_w, max_dim / body_h)
    nw, nh = int(body_w * scale), int(body_h * scale)
    scaled = composed.resize((nw, nh), Image.LANCZOS)

    out = Image.new("RGBA", (size, size), (*bg_color, 255))
    x = (size - nw) // 2
    y = (size - nh) // 2
    out.paste(scaled, (x, y), scaled)
    return out.convert("RGB")


# ---------------------------------------------------------------------------
# Where the GLASS is inside a device-art capture — measured, not guessed.
#
# When the simulator runs in device-art mode the brightness detectors in
# `geometry` are working against them: outside the true display lies the black
# BEZEL, not bright chrome, so the chrome detector sizes itself to the whole
# body and the near-black detector's "equator" wanders with whatever the screen
# happens to show. Both were observed on real venu3 captures: the dark detector
# put the display centre ~30 px above the glass on an ordinary frame, and on
# bright pages (a QR block, a big white clock) it lost the display outright and
# the capture came back framed inside a second watch body — the ghost bezel
# ring with shrunken, off-centre content.
#
# The way out is that the sim's device art IS Garmin's own SDK device PNG.
# That PNG ships the body and the screen cut-out together, so the ratio between
# them is a FIXED property of the device, not of the screenshot: measure the
# body in the capture, scale the PNG's screen rectangle by the same factor, and
# the glass is located exactly — with no reference to a single screen pixel.
# ---------------------------------------------------------------------------

#: Rows whose body extent is within this fraction of the widest are the body's
#: "equator plateau". Its MIDPOINT is the body centre: on a round watch the
#: silhouette is widest over a band of rows, and taking the first or the widest
#: single row lands anywhere in that band (±40 px on venu3). The midpoint does
#: not — on the venu3 SDK PNG it reproduces the screen-rect centre exactly.
_BODY_PLATEAU = 0.995

_sdk_metrics_cache = {}


def _row_extents(mask, y0, y1):
    """Per-row (extent, centre-x) of `mask` between rows y0..y1, measured from
    the OUTERMOST set pixel on each side.

    Outer boundary, deliberately: it is the body silhouette, and unlike the
    "longest contiguous run" used for the crop box it cannot be split by what
    the screen is showing. A white QR block across the middle of the display
    erases the equator rows from a contiguous-run measurement — exactly the
    rows the body centre is read from.
    """
    n = mask.shape[0]
    ext = np.full(n, -1.0)
    cxs = np.zeros(n)
    for y in range(max(0, y0), min(n, y1)):
        xr = np.where(mask[y])[0]
        if xr.size < 20:
            continue
        ext[y] = float(xr[-1] - xr[0])
        cxs[y] = (float(xr[0]) + float(xr[-1])) / 2.0
    return ext, cxs


def _plateau_metrics(ext, cxs):
    """(widest extent, centre x, centre y) from per-row extents, or None."""
    if ext.max() <= 0:
        return None
    plateau = np.where(ext >= ext.max() * _BODY_PLATEAU)[0]
    if plateau.size == 0:
        return None
    return (float(ext.max()),
            float(np.median(cxs[plateau])),
            (float(plateau.min()) + float(plateau.max())) / 2.0)


def _sdk_body_metrics(device_id):
    """Body-to-screen geometry of a device, read once from its SDK device PNG.

    Returns ``(body_w, body_cx, body_cy, screen_cx, screen_cy, screen_r)`` in
    PNG pixels, or None when the device ships no PNG. Cached: the PNG never
    changes within a run and the flood fill is not free.
    """
    if device_id in _sdk_metrics_cache:
        return _sdk_metrics_cache[device_id]
    result = None
    path = _sdk_device_png_path(device_id)
    if path is not None:
        try:
            png = Image.open(path).convert("RGBA")
            arr = np.array(png)
            alpha = arr[:, :, 3].copy()
            alpha[_sdk_workspace_mask(arr)] = 0
            body = alpha > 0
            ext, cxs = _row_extents(body, 0, body.shape[0])
            metrics = _plateau_metrics(ext, cxs)
            # The screen is the transparent cut-out — the SAME rectangle the
            # framer pastes the face into, so this geometry and the composite
            # can never disagree about where the display is.
            sx0, sy0, sx1, sy1 = _detect_sdk_screen_rect(png)
            if metrics is not None:
                body_w, body_cx, body_cy = metrics
                result = (body_w, body_cx, body_cy,
                          (sx0 + sx1) / 2.0, (sy0 + sy1) / 2.0,
                          (sx1 - sx0) / 2.0)
        except Exception:
            log.debug("could not read SDK body metrics for %s", device_id,
                      exc_info=True)
            result = None
    _sdk_metrics_cache[device_id] = result
    return result


def device_art_screen_circle(img, device_id, device_res=None):
    """(cx, cy, radius) of the DISPLAY in a device-art capture, or None.

    None means "not applicable, use a brightness detector": the capture is not
    device art, the device ships no SDK PNG, or the measurement came out
    implausible. It never means "no display here".

    Content-independent by construction — nothing inside the glass is read.
    """
    a = np.array(img.convert("RGB"))
    corner = _corner_brightness(a.astype(np.float32).mean(2))
    if corner < 180:
        return None                      # no white canvas -> not device art

    sdk = _sdk_body_metrics(device_id)
    if sdk is None:
        return None
    sdk_w, sdk_cx, sdk_cy, scr_cx, scr_cy, scr_r = sdk

    # Same two-pass mask as detect_device_art_geometry: dark colorways first,
    # then "anything meaningfully darker than the canvas" for ivory ones.
    thresholds = [WATCH_DARK_MAX]
    light = int(corner) - CANVAS_CONTRAST
    if light > WATCH_DARK_MAX:
        thresholds.append(light)
    for thr in thresholds:
        watch = a.min(2) < thr
        span = _watch_row_span(watch)
        if span is None:
            continue
        metrics = _plateau_metrics(*_row_extents(watch, *span))
        if metrics is None:
            continue
        body_w, body_cx, body_cy = metrics
        scale = body_w / sdk_w
        r = scr_r * scale
        if r < MIN_DISPLAY_RADIUS:
            continue
        if device_res:
            # The simulator renders at an integer DPI scale; anything else means
            # the body was mis-measured (a dialog overlapping it, a clipped
            # window). Fall back rather than crop a wrong rectangle.
            s = (2.0 * r) / device_res
            if round(s) not in (1, 2) or abs(s - round(s)) > 0.12:
                continue
        cx = body_cx + (scr_cx - sdk_cx) * scale
        cy = body_cy + (scr_cy - sdk_cy) * scale
        log.debug("device-art glass: body %.0f px (%.2fx SDK) -> "
                  "centre=(%.0f,%.0f) r=%.0f", body_w, scale, cx, cy, r)
        return (int(round(cx)), int(round(cy)), int(round(r)))
    return None


def find_display_circle(img, device_id=None, device_res=None):
    """Locate the round display in a simulator capture — the one detector
    callers should use.

    Prefers :func:`device_art_screen_circle`, which measures the sim's own
    watch body against the SDK device PNG and never reads a screen pixel, and
    falls back to the brightness-based :func:`find_watch_face_auto` when the
    capture is not device art or the device ships no PNG.
    """
    if device_id:
        circle = device_art_screen_circle(img, device_id, device_res)
        if circle is not None:
            return circle
    return find_watch_face_auto(img, device_res)


def looks_like_app_loaded(img, device_id=None, device_res=None):
    """Did the simulator actually load an app UI? Judged on the DISPLAY.

    :func:`~ciqkit.capture.geometry._looks_like_app_loaded` answers this from
    pixel statistics, and callers hand it the whole simulator WINDOW. In
    device-art mode most of that window is the sim's white canvas, so its
    median brightness sits just under the "this is not an app" cut-off and any
    bright page pushes it over: a venu3 window reads 49 on an ordinary
    watchface and 88 on the QR page (the cut-off is 60). The push then reports
    "app did not load" for a screen that loaded perfectly, restarts the
    simulator, and tries again -- three times, and on the third it keeps the
    frame anyway. Each of those restarts also re-ran face detection, which is
    how one bad detection got into an otherwise good batch.

    So when the glass can be MEASURED (see :func:`device_art_screen_circle`),
    judge the pixels inside it. The same window reads a median of 0 there on
    every page, bright or not. With no measurement available the behaviour is
    unchanged -- the full image, as before.
    """
    circle = device_art_screen_circle(img, device_id, device_res) if device_id else None
    if circle is not None:
        cx, cy, r = circle
        img = img.crop((max(0, cx - r), max(0, cy - r),
                        min(img.width, cx + r), min(img.height, cy + r)))
    return _looks_like_app_loaded(img)
