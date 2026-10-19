"""Watch-face geometry: where the round display is in a simulator screenshot.

Pure image analysis -- no window handles, no subprocesses, no platform calls.
That is the point of it being its own module: this is the half of the capture
layer CI can actually exercise (see ``tests/test_capture_geometry.py``), and
keeping it free of GUI plumbing is what lets those tests run headless on Linux.

The detector answers one question -- ``(cx, cy, radius)`` of the watch face --
and everything else here either crops to that circle or reduces a frame to a
signature cheap enough to compare frames with.

Imported wholesale by :mod:`ciqkit.capture.screenshot_utils`, which remains the
single entry point callers use.
"""
from __future__ import annotations

import logging

import numpy as np
from PIL import Image

log = logging.getLogger("ciqkit.capture")


DARK_THRESH = 5          # Bezel ring spikes to 7-50; display stays < 5
MIN_DISPLAY_WIDTH = 150  # Smallest device (360px) renders ~720px wide


def find_watch_face(img, device_res=None, content_thresh=None):
    """Detect the circular watch face display area.

    Returns (cx, cy, radius) in image pixel coordinates, or None.

    content_thresh: pixel-brightness cutoff that separates the display disc from
    the simulator chrome. Default (None → DARK_THRESH) treats the display as
    NEAR-BLACK, which is correct for watch-APP screens. Watchfaces with a bright
    region (e.g. a sunset sky) break that assumption — the bright rows carry no
    black run, so the circle gets fitted to the lower dark portion only and the
    crop is mis-centered. find_watch_face_chrome() passes a HIGH threshold (just
    under the white chrome) so the disc is detected from the chrome boundary
    instead, independent of interior content.
    """
    thresh = DARK_THRESH if content_thresh is None else content_thresh
    arr  = np.array(img)[:, :, :3].astype(np.float32)
    h, w = arr.shape[:2]
    gray = np.mean(arr, axis=2)

    raw_rows = []
    for y in range(h):
        row  = gray[y, :]
        dark = row < thresh
        padded = np.concatenate(([False], dark, [False]))
        diffs  = np.diff(padded.astype(np.int8))
        starts = np.where(diffs == 1)[0]
        ends   = np.where(diffs == -1)[0]
        if len(starts) == 0:
            continue
        lengths  = ends - starts
        best_idx = int(np.argmax(lengths))
        best_len = int(lengths[best_idx])
        if best_len >= MIN_DISPLAY_WIDTH:
            left = int(starts[best_idx])
            right = left + best_len - 1
            if best_len >= w * 0.9:
                continue
            raw_rows.append((left, right, y, best_len))

    if len(raw_rows) < 20:
        log.debug("no display edges detected; caller will fall back")
        return None

    widths    = np.array([r[3] for r in raw_rows])
    ref_width = int(np.percentile(widths, 90))
    display_rows = [r for r in raw_rows if r[3] >= ref_width * 0.50]

    if len(display_rows) < 20:
        log.debug("no display edges detected after filtering; caller will fall back")
        return None

    widths  = np.array([r[3] for r in display_rows])
    ref_max = int(np.percentile(widths, 95))
    equator_rows = [r for r in display_rows
                    if r[3] >= ref_max * 0.97 and r[3] <= ref_max * 1.03]
    if len(equator_rows) < 3:
        equator_rows = [r for r in display_rows if r[3] >= ref_max * 0.90]

    h_diam  = int(np.median([r[3]              for r in equator_rows]))
    h_cx    = int(np.median([(r[0] + r[1]) / 2 for r in equator_rows]))
    face_cy = int(np.median([r[2]              for r in equator_rows]))
    h_r     = h_diam // 2

    snapped_r = None
    if device_res:
        scale = h_diam / device_res
        log.debug("face: center=(%d,%d) r=%d diameter=%d scale=%.2fx",
                  h_cx, face_cy, h_r, h_diam, scale)
        if scale < 0.8 or scale > 2.5:
            log.warning(f"unusual scale {scale:.2f}x "
                  f"(expected ~1.0x or ~2.0x for {device_res}px device)")
        # The simulator renders at an integer DPI scale (1x Retina-off, 2x
        # Retina-on), so when the detected scale is close to an integer the
        # true display radius is exactly device_res*scale/2. Snapping fixes
        # small under-estimates from bright content at the display border AND
        # bounds the bright-edge expansion below, which otherwise walks out to
        # the device-art body edge and swallows bezel + straps (watch-in-watch
        # store screenshots).
        near = round(scale)
        if near >= 1 and abs(scale - near) <= 0.15:
            snapped_r = int(device_res * near / 2)
            if snapped_r != h_r:
                log.debug(f"  Radius snapped {h_r} → {snapped_r} ({near}x scale, {device_res}px device)")
                h_r = snapped_r
    else:
        log.debug(f"  Face: center=({h_cx},{face_cy}) r={h_r}")

    # ------------------------------------------------------------------
    # Radius expansion: bright edge content (e.g. an orange progress ring
    # painted at the display border) is not "dark" so the dark-pixel scan
    # above under-estimates the radius.  Scan from the OUTSIDE INWARD along
    # several equator rows to find the outermost non-background pixel — that
    # is the physical watch-body edge and is always >= the display radius.
    # Only applied when the image has a bright background (macOS simulator
    # chrome is white; on a dark background the dark-pixel radius is correct).
    # ------------------------------------------------------------------
    corner_brightness = float(np.mean([
        gray[:20, :20].mean(),  gray[:20, -20:].mean(),
        gray[-20:, :20].mean(), gray[-20:, -20:].mean(),
    ]))
    if corner_brightness > 60:
        bg_thresh   = corner_brightness * 0.80
        scan_limit  = int(h_r * 1.5)
        sample_ys   = [face_cy + dy for dy in (0, -4, 4, -8, 8)
                       if 0 <= face_cy + dy < h]
        right_hits, left_hits = [], []
        for sy in sample_ys:
            row_g = gray[sy, :]
            for x in range(min(w - 1, h_cx + scan_limit), h_cx, -1):
                if row_g[x] < bg_thresh:
                    right_hits.append(x)
                    break
            for x in range(max(0, h_cx - scan_limit), h_cx):
                if row_g[x] < bg_thresh:
                    left_hits.append(x)
                    break
        if right_hits and left_hits:
            scan_r = min(int(np.median(right_hits)) - h_cx,
                         h_cx - int(np.median(left_hits)))
            # The scan finds the outermost non-background pixel — the BODY
            # edge, not the display edge. With simulator device art the body
            # extends far past the display. When the radius was snapped to
            # the device resolution above it is already exact — never expand
            # past it. Without device_res, cap the growth at 15% (legit
            # under-estimates from a bright border ring are small).
            if snapped_r is not None:
                scan_r = min(scan_r, snapped_r)
            else:
                scan_r = min(scan_r, int(h_r * 1.15))
            if scan_r > h_r:
                log.debug(f"  Radius expanded {h_r} → {scan_r} (bright-edge scan)")
                h_r = scan_r

    return (h_cx, face_cy, h_r)


def find_watch_face_chrome(img, device_res=None):
    """Content-independent watch-face detection for WATCHFACES.

    A watchface fills the whole round display, and any part of it may be bright
    (sunset skies, light themes), so the near-black detector in find_watch_face
    can't be trusted. Here we detect the disc by the simulator's bright white
    CHROME instead: sample the corner brightness, set a threshold just below it,
    and treat every pixel darker than that (i.e. NOT chrome) as display. Returns
    (cx, cy, radius) or None if the corners aren't bright chrome (e.g. a real
    device screenshot with no chrome), in which case the caller should fall back
    to find_watch_face.
    """
    arr  = np.array(img)[:, :, :3].astype(np.float32)
    h, w = arr.shape[:2]
    gray = np.mean(arr, axis=2)
    corner = float(np.mean([
        gray[:20, :20].mean(),  gray[:20, -20:].mean(),
        gray[-20:, :20].mean(), gray[-20:, -20:].mean(),
    ]))
    # Chrome must be clearly bright to key off it; bail otherwise.
    if corner < 140:
        return None
    # Threshold 80% of chrome brightness: chrome stays above, all display
    # content (even a bright orange band ~150) falls below.
    return find_watch_face(img, device_res, content_thresh=corner * 0.80)


def _corner_brightness(gray):
    """Mean brightness of the four 20x20 image corners (the sim chrome)."""
    return float(np.mean([
        gray[:20, :20].mean(),  gray[:20, -20:].mean(),
        gray[-20:, :20].mean(), gray[-20:, -20:].mean(),
    ]))


def face_fit_score(gray, corner_bright, cx, cy, r):
    """Geometric quality of a candidate (cx, cy, r) against the chrome boundary.

    Content-independent: samples a ring just INSIDE the circle (should be
    display, i.e. darker than chrome) and just OUTSIDE (should be bright chrome)
    at 72 angles. Returns the fraction of angles where BOTH hold — peaks sharply
    at the true display radius/centre and drops for a mis-sized or mis-centred
    circle (a too-small circle has display, not chrome, just outside it). Only
    meaningful when the chrome is bright; callers gate on that.
    """
    h, w = gray.shape[:2]
    thr = corner_bright * 0.80
    good = 0
    tot  = 0
    for k in range(72):
        a  = 2.0 * np.pi * k / 72.0
        ca = np.cos(a)
        sa = np.sin(a)
        ix = int(cx + 0.90 * r * ca)
        iy = int(cy + 0.90 * r * sa)
        ox = int(cx + 1.12 * r * ca)
        oy = int(cy + 1.12 * r * sa)
        if not (0 <= ix < w and 0 <= iy < h and 0 <= ox < w and 0 <= oy < h):
            continue
        tot += 1
        if gray[iy, ix] < thr and gray[oy, ox] >= thr:
            good += 1
    return (good / tot) if tot else 0.0


def find_watch_face_auto(img, device_res=None, primary=None):
    """Self-healing face detection for ANY screen (watch-app or watchface).

    Runs the near-black detector (or accepts a caller-supplied `primary` result,
    so an app with its own detector can reuse it) and the chrome detector, then
    keeps the primary UNLESS chrome fits the chrome boundary clearly better. The
    +0.10 margin means an already-good detection is returned unchanged (byte-
    identical crops) and only a detection that was fooled by bright interior
    content (sunset sky, colour chart, radar) gets rescued. When there is no
    bright chrome to key on (e.g. a real-device screenshot), the primary is
    returned as-is.
    """
    arr  = np.array(img)[:, :, :3].astype(np.float32)
    gray = np.mean(arr, axis=2)
    corner = _corner_brightness(gray)

    dark = primary if primary is not None else find_watch_face(img, device_res)
    if corner < 140:
        return dark
    chrome = find_watch_face_chrome(img, device_res)
    if chrome is None:
        return dark
    if dark is None:
        return chrome

    # Scale plausibility beats the chrome-boundary fit. The simulator renders
    # at an integer DPI scale, so a candidate whose implied scale is ~1x/2x of
    # device_res is almost certainly the display. This matters when the sim
    # shows DEVICE ART: just outside the true display lies the black bezel
    # (not chrome), so face_fit_score rates the true circle 0.00 while the
    # body-edge circle scores well — the fit comparison alone then swallows
    # bezel + straps into the crop (watch-in-watch store screenshots).
    def _scale_plausible(cand):
        if not device_res:
            return None
        s = (2.0 * cand[2]) / device_res
        # The simulator only renders at 1x (Retina off) or 2x (Retina on).
        return round(s) in (1, 2) and abs(s - round(s)) <= 0.12
    dark_ok = _scale_plausible(dark)
    chrome_ok = _scale_plausible(chrome)
    if dark_ok and chrome_ok is False:
        return dark
    if chrome_ok and dark_ok is False:
        log.debug("  Face auto-fallback: chrome candidate is scale-plausible, dark is not — using chrome")
        return chrome

    sd = face_fit_score(gray, corner, *dark)
    sc = face_fit_score(gray, corner, *chrome)
    if sc > sd + 0.10:
        log.debug(f"  Face auto-fallback: chrome fit {sc:.2f} > dark {sd:.2f} — using chrome")
        return chrome
    return dark


def crop_face(img, cx, cy, radius, native_res=None, pad=2):
    """Crop the circular watch face; optionally resize to native resolution."""
    half    = radius + pad
    cropped = img.crop((cx - half, cy - half, cx + half, cy + half))
    if native_res:
        cropped = cropped.resize((native_res, native_res), Image.LANCZOS)
    return cropped


def crop_watch_body(img, face_cx, face_cy, face_radius, pad=12):
    """Crop the full watch body (bezel + band) from a simulator screenshot."""
    arr   = np.array(img.convert("RGB")).astype(np.float32)
    is_bg = np.all(arr > 232, axis=2)

    col_x       = max(0, min(int(face_cx), img.width - 1))
    face_top_y  = max(0, int(face_cy - face_radius))
    face_bot_y  = min(img.height - 1, int(face_cy + face_radius))

    top = face_top_y
    for y in range(face_top_y, -1, -1):
        if is_bg[y, col_x]:
            top = y + 1
            break

    hardware_zone = int(face_radius * 0.5)
    bottom = face_bot_y
    for y in range(face_bot_y, img.height):
        if is_bg[y, col_x]:
            break
        if y > face_bot_y + hardware_zone:
            if float(arr[y, col_x].mean()) > 80:
                break
        bottom = y
    bottom = max(bottom, face_bot_y)

    h_margin = int(face_radius * 1.32) + pad
    left = max(0, int(face_cx) - h_margin)
    r = min(img.width, int(face_cx) + h_margin + 1)
    t = max(0, top - pad)
    b = min(img.height, bottom + 1)
    return img.crop((left, t, r, b))

def _looks_like_app_loaded(img) -> bool:
    """Heuristic: does the image show an actual app UI (not blank/glance/menu)?"""
    if img is None or img.size[0] < 100 or img.size[1] < 100:
        return False
    arr  = np.array(img.convert("RGB"))
    h, w = arr.shape[:2]
    gray = np.mean(arr, axis=2)
    if float(np.median(gray)) > 60:
        return False
    bottom = gray[h // 2:, :]
    return float(np.mean(bottom > 180)) > 0.003


def face_region_signature(img, cx: int, cy: int, radius: int) -> tuple:
    """Return a coarse signature of the watch-face center region.

    Used to detect that the displayed page actually changed after a swipe/click.
    The average brightness of a 4x4 grid over the face (16 values), followed
    by the same over a 16x16 grid (256 values). The coarse grid sees a page
    swap; the fine one sees what a coarse mean dilutes away -- text REPLACED
    IN PLACE ("MINI OK" -> "HELD" moved no 4x4 cell by more than 9.4), which
    is exactly what a confirmed tap or hold often produces.
    """
    arr = np.array(img.convert("RGB"), dtype=np.float32)
    h, w = arr.shape[:2]
    gray = np.mean(arr, axis=2)
    r = max(8, int(radius * 0.7))
    y0 = max(0, int(cy) - r)
    y1 = min(h, int(cy) + r)
    x0 = max(0, int(cx) - r)
    x1 = min(w, int(cx) + r)
    region = gray[y0:y1, x0:x1]
    if region.size == 0:
        return tuple([0.0] * (16 + 256))
    return tuple(_grid_means(region, 4) + _grid_means(region, 16))


def _grid_means(region, n: int) -> list:
    rh, rw = region.shape
    cells = []
    for gy in range(n):
        for gx in range(n):
            ya = (rh * gy) // n
            yb = (rh * (gy + 1)) // n
            xa = (rw * gx) // n
            xb = (rw * (gx + 1)) // n
            cell = region[ya:yb, xa:xb]
            cells.append(float(np.mean(cell)) if cell.size else 0.0)
    return cells



def frame_detail_signature(img, grid: int = 48) -> tuple:
    """Mean brightness of a ``grid`` x ``grid`` split of the WHOLE frame, for
    :func:`details_differ`.

    Deliberately not tied to the detected face. Face detection assumes a round
    display: on a rectangular one (venusq2) it measured the dark device body
    instead (an "unusual scale 3.07x"), so a face-relative region sat partly
    on the bezel and its cells were too coarse for one placed mark to register
    -- the press was judged a miss and re-sent, a second move. Everything in a
    simulator capture outside the display is static chrome and device art, so
    diffing the whole frame needs no geometry at all.
    """
    arr = np.asarray(img.convert("RGB"), dtype=np.float32)
    gray = np.mean(arr, axis=2)
    h, w = gray.shape
    if h < grid or w < grid:
        return ()
    cells = []
    for gy in range(grid):
        ya, yb = (h * gy) // grid, (h * (gy + 1)) // grid
        for gx in range(grid):
            xa, xb = (w * gx) // grid, (w * (gx + 1)) // grid
            cells.append(float(gray[ya:yb, xa:xb].mean()))
    return tuple(cells)


def details_differ(sig_a: tuple, sig_b: tuple, cell_threshold: float = 24.0,
                   min_cells: int = 3) -> bool:
    """True if at least ``min_cells`` cells of two detail signatures changed
    brightness by ``cell_threshold`` or more.

    The coarse test (:func:`signatures_differ`) averages over the whole face, so
    a real but LOCAL change -- a mark placed on a mostly dark game board, a
    highlight moving one row -- stays far under its threshold. A gesture whose
    effect it missed was then retried, and a retried START press is a second
    move: the check meant to stop confirmed-wrong output produced it. Counting
    strongly changed small cells catches a local change while an antialiasing
    wobble or a uniform near-black shift (a few levels per cell) still does not
    count. Used only to confirm input (``SimSession.act``), never to prove an
    app loaded, where a ticking watch face must not pass for a new app.
    """
    if not sig_a or not sig_b or len(sig_a) != len(sig_b):
        return False
    hits = sum(1 for a, b in zip(sig_a, sig_b, strict=True) if abs(a - b) >= cell_threshold)
    return hits >= min_cells


def signatures_differ(sig_a: tuple, sig_b: tuple, threshold: float = 12.0) -> bool:
    """True if the screen changed: the per-cell brightness moved by more than
    `threshold` on average, OR at least two cells each moved by twice that.

    The mean alone is tuned so a page swap on a 454px display fires while a
    minor antialiasing tweak does not -- but it dilutes a change that is
    strong and local. Measured on a dark paged app (venu3): swiping from its
    main page to the next one changed the title band by 37 and 39 and every
    other cell by under 10, mean 10.2 -- "no change". The gesture had landed;
    the retry that followed swiped a second page, and a third. The second
    clause catches that shape. It needs TWO strong cells, so a single cell
    flipping (one ticking digit) still does not confirm a gesture.

    A signature from :func:`face_region_signature` also carries a 16x16 grid
    after the 4x4 one: the screen changed if at least 8 of its 256 cells (~3 %
    of the region) each moved by 2x the threshold. Replaced text clears that
    by a wide margin; a single ticking seconds digit covers ~3-6 such cells
    and does not.
    """
    if not sig_a or not sig_b or len(sig_a) != len(sig_b):
        return False
    coarse_a, coarse_b = sig_a[:16], sig_b[:16]
    diffs = [abs(a - b) for a, b in zip(coarse_a, coarse_b, strict=True)]
    if (sum(diffs) / len(diffs)) >= threshold:
        return True
    if sum(1 for d in diffs if d >= 2 * threshold) >= 2:
        return True
    fine = [abs(a - b) for a, b in zip(sig_a[16:], sig_b[16:], strict=True)]
    return sum(1 for d in fine if d >= 2 * threshold) >= 8
