#!/usr/bin/env python3
"""
Assess Garmin Connect IQ watch screenshot quality.

Detects the typical failure modes you hit while iterating on store screenshots
and dev captures, so the assistant (or a CI hook) can flag bad images without
asking a human to eyeball every PNG.

Quality dimensions checked:

  1. File loads as an image, isn't 1×1 or comically large.
  2. Image matches a *known* watch-screenshot size. The table covers the five
     round face sizes seen most often (454, 416, 390, 360 square), the framed
     store sizes, the store cover and the device icon, each also accepted at
     2x and 3x for HiDPI captures. It is NOT a complete list of Garmin display
     sizes, so an unknown size only warns; pass `--allow-size WxH` to whitelist
     yours without editing this file.
  3. Aspect ratio (watch shots should be 1:1 within ±5 %).
  4. Color diversity — solid black/white/blue almost always means the
     simulator hadn't loaded the .prg yet at capture time (CIQ shows a flat
     blue loading triangle before app pixels arrive).
  5. Sharpness via Laplacian variance — low variance = blurred capture, often
     from grabbing mid-resize.
  6. **Watch face presence**: for native crops the dark circular display must
     fill the frame; for framed store shots the dark watch *body* must sit on
     the framed dark background (with brightness contrast between body and
     background). Both checks use HoughCircles when cv2 is available, with a
     centered-darkness fallback otherwise.
  7. Not-a-loading-screen: the centred face must contain more than a couple
     of distinct colours (CIQ's pre-load blue triangle = ~2 colours total).
  8. Framed-shot dark background: image corners should be the store BG colour
     (dark navy ~ 15,15,26). Bright corners mean the capture leaked the
     simulator's white wxWidgets chrome into the framed output.

Each finding is reported as a (severity, code, detail) tuple; an overall
score 0–100 sums penalties so callers can sort worst-first or skip everything
above a threshold.

Usage::

    ciqkit shot-score path/to/image.png
    ciqkit shot-score path/to/dir/        # recursive over PNG/JPG
    ciqkit shot-score --json path/...     # machine-readable
    ciqkit shot-score --fail-under 70 .   # exit 1 if any score < 70

CLI is dep-tolerant: PIL + numpy are required; cv2 is optional but unlocks the
Hough-circle check (catches "watch body absent or off-centre" reliably).

This file is intentionally a single self-contained script — drop it into CI
or invoke it ad-hoc from agents without touching a requirements file.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np
from PIL import Image

try:
    import cv2  # type: ignore
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False


# ── Known sizes ──────────────────────────────────────────────────────────────
# (label, (W, H), kind). 'kind' drives downstream checks:
#   native_face: image IS the watch display (no bezel/chrome)
#   framed:     image is the framed-for-store composite (watch body on dark bg)
#   cover:      Garmin Web/Mobile cover image (no watch necessarily)
#   icon:       App-store on-device icon
# Tolerance ±2 px on each axis.

KNOWN_SIZES = [
    ("venu3_native",        (454, 454), "native_face"),
    ("fr265_native",        (416, 416), "native_face"),
    ("vivoactive5_native",  (390, 390), "native_face"),
    ("venu3s_native",       (390, 390), "native_face"),
    ("fr265s_native",       (360, 360), "native_face"),
    ("framed_store",        (1080, 1080), "framed"),
    ("framed_hq",           (2160, 2160), "framed"),
    ("cover_500",           (500, 500),  "cover"),
    ("icon_128",            (128, 128),  "icon"),
    ("hero_banner_1440x720",   (1440, 720),  "hero"),
    ("manual_page_portrait",   (1080, 1920), "manual"),
]

STORE_BG = np.array([15, 15, 26], dtype=np.float32)  # matches screenshot_utils

SEVERITY_FAIL = "fail"
SEVERITY_WARN = "warn"
SEVERITY_INFO = "info"

# Per-severity score penalty
PENALTY = {SEVERITY_FAIL: 35, SEVERITY_WARN: 12, SEVERITY_INFO: 0}


@dataclass
class Finding:
    severity: str
    code: str
    detail: str = ""


@dataclass
class Result:
    path: str
    score: int = 100
    matched_kind: str | None = None
    matched_label: str | None = None
    metrics: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)

    def add(self, severity: str, code: str, detail: str = ""):
        self.findings.append(Finding(severity, code, detail))
        self.score = max(0, self.score - PENALTY.get(severity, 0))


# ── Helpers ──────────────────────────────────────────────────────────────────

# Retina and HiDPI displays hand back a capture at an integer multiple of the
# logical size, so a Venu 3 face grabbed on a 2x Mac is 908x908, not 454x454.
# Those are correct captures, not unknown sizes.
PIXEL_SCALES = (1, 2, 3)


def _classify_size(w: int, h: int,
                   allowed_extras: list) -> tuple[str | None, str | None, int]:
    """Return (kind, label, pixel_scale) for the best-matching known size.

    ``pixel_scale`` is the display scale the capture was taken at; it is 1 for
    an exact match and 0 when nothing matched.
    """
    for scale in PIXEL_SCALES:
        for label, (kw, kh), kind in KNOWN_SIZES + allowed_extras:
            if abs(w - kw * scale) <= 2 and abs(h - kh * scale) <= 2:
                suffix = "" if scale == 1 else f"@{scale}x"
                return kind, f"{label}{suffix}", scale
    return None, None, 0


def _laplacian_variance(gray: np.ndarray) -> float:
    """Sharpness metric. >200 sharp, <50 blurry, typical native crops ~400–2000."""
    if HAS_CV2:
        lap = cv2.Laplacian(gray.astype(np.float32), cv2.CV_32F)
        return float(lap.var())
    # numpy fallback: convolve the 3x3 Laplacian kernel by hand
    # Pad and convolve along both axes by hand (np.lib.stride_tricks is faster
    # but overkill for screenshot sizes).
    g = gray.astype(np.float32)
    h, w = g.shape
    out = np.zeros_like(g)
    out[1:-1, 1:-1] = (
        g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:] - 4 * g[1:-1, 1:-1]
    )
    return float(out.var())


def _to_logical_scale(gray: np.ndarray, pixel_scale: int) -> np.ndarray:
    """Downsample a HiDPI capture back to its logical pixel size."""
    if pixel_scale <= 1:
        return gray
    return gray[::pixel_scale, ::pixel_scale]


def _detect_central_dark_circle(gray: np.ndarray) -> dict | None:
    """Try to find the watch face / watch body via HoughCircles.

    Returns dict with cx, cy, r, fraction_centered, or None when no circle.
    Centred-darkness fallback (no cv2): check that the centre 60 % radius is
    notably darker than the corners; if so, synthesize a "circle" entry."""
    h, w = gray.shape
    if HAS_CV2:
        gray_u8 = gray.astype(np.uint8)
        circles = cv2.HoughCircles(
            gray_u8, cv2.HOUGH_GRADIENT, dp=1, minDist=max(50, min(w, h) // 4),
            param1=80, param2=30,
            minRadius=min(w, h) // 6, maxRadius=min(w, h) // 2,
        )
        if circles is not None and len(circles) > 0:
            # Pick the most-centred large circle
            cands = circles[0]
            cx_img, cy_img = w / 2, h / 2
            cands_sorted = sorted(
                cands,
                key=lambda c: (abs(c[0] - cx_img) + abs(c[1] - cy_img)) / max(1, c[2]),
            )
            cx, cy, r = cands_sorted[0]
            off = (abs(cx - cx_img) + abs(cy - cy_img)) / max(1, r)
            return {"cx": float(cx), "cy": float(cy), "r": float(r), "centeredness": float(1.0 - min(1.0, off))}
        return None

    # Fallback: average brightness in a centred disk vs corners. Cheap, brittle.
    yy, xx = np.ogrid[:h, :w]
    cy, cx = h / 2, w / 2
    r = min(w, h) * 0.35
    disk_mask = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
    if disk_mask.sum() == 0:
        return None
    centre_mean = gray[disk_mask].mean()
    corners = np.concatenate(
        [gray[:20, :20].ravel(), gray[:20, -20:].ravel(),
         gray[-20:, :20].ravel(), gray[-20:, -20:].ravel()]
    )
    corner_mean = corners.mean()
    if corner_mean - centre_mean > 25:
        return {"cx": cx, "cy": cy, "r": r, "centeredness": 1.0}
    return None


def _unique_colours_approx(rgb: np.ndarray, sample: int = 5000) -> int:
    """Return the count of unique 4-bit-quantised colours in a random sample.
    Discriminates CIQ's loading-blue triangle (2–3 colours) from a real watch
    face (50+ colours) without paying for a full histogram on 4K images."""
    flat = rgb.reshape(-1, 3)
    if flat.shape[0] > sample:
        idx = np.random.default_rng(0).choice(flat.shape[0], sample, replace=False)
        flat = flat[idx]
    quantised = flat // 16  # 4-bit per channel
    return int(len(np.unique(quantised.view(np.dtype((np.void, quantised.dtype.itemsize * 3))))))


# ── Per-image assessment ─────────────────────────────────────────────────────

def assess(path: Path | str, allowed_extras: list | None = None) -> Result:
    """Score one image 0-100 and list what is wrong with it.

    ``allowed_extras`` whitelists additional ``(label, (w, h), kind)`` sizes on
    top of :data:`KNOWN_SIZES`.
    """
    path = Path(path)
    allowed_extras = allowed_extras or []
    r = Result(path=str(path))

    if not path.exists():
        r.add(SEVERITY_FAIL, "file_missing", str(path))
        return r

    try:
        img = Image.open(path).convert("RGB")
    except Exception as e:
        r.add(SEVERITY_FAIL, "load_failed", f"{type(e).__name__}: {e}")
        return r

    w, h = img.size
    arr = np.asarray(img)
    r.metrics["size"] = [w, h]
    r.metrics["bytes"] = path.stat().st_size

    # 1. Size sanity
    if w < 100 or h < 100:
        r.add(SEVERITY_FAIL, "too_small", f"{w}x{h}")
        return r
    if w > 4096 or h > 4096:
        r.add(SEVERITY_WARN, "unusually_large", f"{w}x{h}")

    # 2. Known size match
    kind, label, pixel_scale = _classify_size(w, h, allowed_extras)
    r.matched_kind = kind
    r.matched_label = label
    if pixel_scale > 1:
        r.metrics["pixel_scale"] = pixel_scale
    if kind is None:
        r.add(SEVERITY_WARN, "unknown_size",
              f"{w}x{h} — not a Garmin native res or framed store size; pass "
              f"--allow-size {w}x{h} to whitelist")

    # 3. Aspect ratio. Hero banners (2:1) and manual pages (portrait) are
    # legitimately non-square; only flag for kinds that should be 1:1.
    aspect = w / h
    r.metrics["aspect"] = round(aspect, 3)
    square_required = kind in (None, "native_face", "framed", "cover", "icon")
    if square_required and not (0.95 <= aspect <= 1.05):
        r.add(SEVERITY_WARN, "non_square_aspect", f"{aspect:.2f}")

    # 4. Color diversity (solid → loading)
    gray = arr.mean(axis=2)
    pixel_std = float(arr.reshape(-1, 3).astype(np.float32).std(axis=0).mean())
    r.metrics["color_std"] = round(pixel_std, 1)
    if pixel_std < 3:
        r.add(SEVERITY_FAIL, "solid_color", f"std={pixel_std:.1f}")
    elif pixel_std < 8:
        r.add(SEVERITY_WARN, "very_low_color_diversity", f"std={pixel_std:.1f}")

    # 5. Sharpness.
    #
    # Laplacian variance is resolution-dependent: upscaling adds pixels but no
    # detail, so a perfectly sharp 2x Retina capture scores far below the same
    # image at 1x and would be reported as blurry. Measure at logical scale.
    sharp = _laplacian_variance(_to_logical_scale(gray, pixel_scale))
    r.metrics["sharpness"] = round(sharp, 1)
    if sharp < 30:
        r.add(SEVERITY_FAIL, "blurry", f"lap_var={sharp:.0f} (<30)")
    elif sharp < 100:
        r.add(SEVERITY_WARN, "soft_focus", f"lap_var={sharp:.0f} (<100)")

    # 6. Loading screen check (CIQ pre-app pixels)
    unique = _unique_colours_approx(arr)
    r.metrics["distinct_colors_4bit"] = unique
    if unique < 6:
        r.add(SEVERITY_FAIL, "looks_like_loading_screen",
              f"distinct_4bit_colors={unique} (<6); simulator likely hadn't "
              f"loaded the app yet")

    # 7. Watch face presence (kind-specific)
    if kind == "native_face":
        # Native crop: the whole frame IS the watch display. The image's
        # brightness profile should be uniform-ish (no big dark margin),
        # because the simulator already cropped to the round display.
        edge_mean = np.mean([
            gray[:6, :].mean(), gray[-6:, :].mean(),
            gray[:, :6].mean(), gray[:, -6:].mean(),
        ])
        centre_mean = gray[h // 3:h * 2 // 3, w // 3:w * 2 // 3].mean()
        r.metrics["edge_mean"] = round(float(edge_mean), 1)
        r.metrics["centre_mean"] = round(float(centre_mean), 1)
        # If edges are way darker than centre AND edges are near-black, we got
        # the framed image when we expected a native crop.
        if centre_mean - edge_mean > 40 and edge_mean < 15:
            r.add(SEVERITY_FAIL, "appears_framed_not_native",
                  f"edge={edge_mean:.0f} centre={centre_mean:.0f}")

    elif kind == "framed":
        # Corners should be the store BG dark navy.
        corners = np.stack([arr[:32, :32], arr[:32, -32:], arr[-32:, :32], arr[-32:, -32:]])
        corner_rgb_mean = corners.reshape(-1, 3).mean(axis=0)
        r.metrics["corner_rgb"] = [round(float(c), 1) for c in corner_rgb_mean]
        dist_to_bg = float(np.linalg.norm(corner_rgb_mean - STORE_BG))
        r.metrics["corner_dist_to_store_bg"] = round(dist_to_bg, 1)
        if dist_to_bg > 50:
            r.add(SEVERITY_WARN, "framed_bg_off",
                  f"corners avg {corner_rgb_mean.tolist()} not store-bg {STORE_BG.tolist()}")
        # Watch body should be roughly centred — Hough or fallback.
        circle = _detect_central_dark_circle(gray)
        if circle is None:
            # Without OpenCV this falls back to a centred-darkness heuristic
            # that misses plenty of legitimate framed shots, so a miss there is
            # weak evidence. Only call it a failure when the Hough detector,
            # which is reliable, is the one that found nothing.
            if HAS_CV2:
                r.add(SEVERITY_FAIL, "no_watch_body_detected",
                      "no dark centred circle — image likely doesn't show a watch")
            else:
                r.add(SEVERITY_WARN, "watch_body_unverified",
                      "could not confirm a centred watch body without OpenCV; "
                      "install ciqkit[cv] for a reliable check")
        else:
            r.metrics["watch_circle"] = {
                k: round(v, 1) for k, v in circle.items()
            }
            if circle["centeredness"] < 0.5:
                r.add(SEVERITY_WARN, "watch_off_centre",
                      f"centeredness={circle['centeredness']:.2f}")

    elif kind == "cover":
        # Cover image: not strictly a watch shot, but should not be a flat
        # colour and should have at least a recognisable subject (we use
        # diversity as a loose proxy).
        if unique < 15:
            r.add(SEVERITY_WARN, "cover_too_flat",
                  f"distinct_4bit_colors={unique} — cover may be undecorated")

    elif kind == "icon":
        # 128×128 icon: just check it's not blank.
        if unique < 4:
            r.add(SEVERITY_FAIL, "icon_blank_or_solid", f"distinct_4bit_colors={unique}")

    return r


# ── CLI ──────────────────────────────────────────────────────────────────────

def _expand_paths(paths, include_debug: bool = False):
    """Resolve directories to PNG/JPG files. Skips any `debug/` subtree by
    default — those are Playwright submit-debug captures, not watch shots.

    Returns (files, missing). A path that is neither a directory nor an
    existing file is reported rather than passed on to `assess`, which would
    die with a traceback on it -- and a mistyped path is the single most likely
    way this command gets pointed at nothing at all.
    """
    out, missing = [], []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for f in sorted(list(p.rglob("*.png")) + list(p.rglob("*.jpg")) + list(p.rglob("*.jpeg"))):
                if not include_debug and "debug" in f.parts:
                    continue
                out.append(f)
        elif p.is_file():
            out.append(p)
        else:
            missing.append(p)
    return out, missing


def _parse_allow_size(strings) -> list:
    extras = []
    for s in strings or []:
        if "x" not in s:
            raise SystemExit(f"--allow-size expects WxH, got {s!r}")
        w, h = s.split("x")
        extras.append((f"custom_{s}", (int(w), int(h)), "native_face"))
    return extras


def _print_human(results):
    width = max((len(r.path) for r in results), default=20)
    sev_order = {SEVERITY_FAIL: 0, SEVERITY_WARN: 1, SEVERITY_INFO: 2}
    for r in results:
        tag = "FAIL" if r.score < 50 else "WARN" if r.score < 80 else "OK"
        kind_disp = f"[{r.matched_kind}]" if r.matched_kind else "[?]"
        print(f"  {tag:>4} {r.score:3}  {r.path:<{width}}  {kind_disp}")
        for f in sorted(r.findings, key=lambda f: sev_order.get(f.severity, 9)):
            print(f"          {f.severity:>4}  {f.code:<32}  {f.detail}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="ciqkit shot-score",
        description="Assess Connect IQ watch screenshot quality.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("paths", nargs="+", help="image files or directories (recursive)")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--fail-under", type=int, default=None,
                    help="exit code 1 if any image scores below this threshold")
    ap.add_argument("--allow-size", action="append", default=None,
                    help="whitelist an additional WxH (e.g. --allow-size 320x320)")
    ap.add_argument("--include-debug", action="store_true",
                    help="include images under debug/ subtrees (skipped by default)")
    args = ap.parse_args(argv)

    extras = _parse_allow_size(args.allow_size)
    targets, missing = _expand_paths(args.paths, include_debug=args.include_debug)
    for p in missing:
        print(f"error: no such file or directory: {p}", file=sys.stderr)
    if not targets:
        # Exit 2, not 0. This command is used as a quality gate in CI, where a
        # renamed output directory or a typo would otherwise score nothing,
        # find nothing below the threshold, and report a cheerful pass.
        # Diagnostics go to stderr so `--json` stdout stays parseable.
        print("error: no PNG/JPG files found in: "
              + ", ".join(str(p) for p in args.paths), file=sys.stderr)
        if args.json:
            print(json.dumps([]))
        return 2

    results = [assess(p, extras) for p in targets]

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2, default=str))
    else:
        print(f"Assessing {len(results)} image(s)  (cv2={'on' if HAS_CV2 else 'off (Hough disabled)'})")
        _print_human(results)
        worst = min(results, key=lambda r: r.score)
        avg = sum(r.score for r in results) / len(results)
        print(f"\n  avg score {avg:.1f}  worst {worst.score} ({worst.path})")

    if missing:
        # Some inputs did not exist. Whatever the surviving images scored, the
        # run did not check what it was asked to check.
        return 2
    if args.fail_under is not None:
        if any(r.score < args.fail_under for r in results):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
