#!/usr/bin/env python3
"""Mechanical layout checks on a captured watch screen.

Complements :mod:`ciqkit.qa.shot_score` (which scores *image* quality -- blur,
size, capture artifacts). This one checks *layout correctness* on a native face
PNG, the failure class that keeps slipping through because Monkey C compiles
regardless of where things land:

  CLIP   ink jammed against / over the round bezel (text getting cut)
  CORNER ink outside the screen circle entirely (drawing into the dead corners)
  BAND   ink outside the round-safe inscribed square (the 22-78%-style safe area)

Round vs rectangular is inferred from the image (square + dark corners => round)
or forced with --rect / --round.

Decorative full-rings (e.g. a progress arc drawn near the edge on purpose) are
NOT flagged as clips: edge ink that wraps most of the way around the rim
(angular coverage >= 70%) is treated as intentional; edge ink concentrated in a
few arcs is treated as a likely text clip. That keeps the check low-false-alarm.

Usage::

  ciqkit layout FACE.png [more.png ...]
  ciqkit layout screenshots/            # all PNGs in a dir
  ciqkit layout --fail-under 80 dir/    # exit 1 if any score < 80
  ciqkit layout --round FACE.png        # force round geometry

This is an ADVISORY score, not a hard gate:
distinguishing clipped text from intentional near-rim content (charts, arcs) is
not reliably possible from pixels alone, so layout problems lower the score and
print a reason for a human to glance at, and the run only exits non-zero when you
set --fail-under. Best signal in practice: a score that DROPS versus a screen's
baseline flags a regression.

Operates on the NATIVE face crop (square-ish, the screen only). Non-native assets
(framed / cover / hero / icon) are skipped — their face is inset so the geometry
doesn't apply. Full-window sim captures should be cropped to the face first
(the simulator emits native-resolution PNGs).
"""

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

INSCRIBED = 0.7071          # half-side / R of the inscribed square (1/sqrt2)
EDGE_FRAC = 0.93            # annulus [EDGE_FRAC*R, R] = "near the rim"
INK_DIST = 60              # color distance from background to count as ink
RING_COVERAGE_OK = 0.70    # >= this angular fraction of rim ink => decorative ring
ANGLE_BINS = 36

PEN = {"FAIL": 40, "WARN": 12, "INFO": 0}


class Result:
    def __init__(self, path):
        self.path = path
        self.score = 100
        self.issues = []
        self.metrics = {}

    def add(self, sev, code, detail=""):
        self.issues.append((sev, code, detail))
        self.score = max(0, self.score - PEN.get(sev, 0))


def _background(rgb):
    """Modal color via coarse 16-level quantization — the page background."""
    q = (rgb // 16).reshape(-1, 3)
    vals, counts = np.unique(q, axis=0, return_counts=True)
    return vals[counts.argmax()] * 16 + 8


def _is_round(rgb, forced):
    if forced in ("round", "rect"):
        return forced == "round"
    h, w, _ = rgb.shape
    if abs(h - w) > max(h, w) * 0.04:
        return False  # clearly rectangular aspect
    # Square: round if the four corners are markedly darker/different from center.
    c = rgb[h // 2 - 3:h // 2 + 3, w // 2 - 3:w // 2 + 3].mean()
    k = max(2, w // 20)
    corners = np.mean([
        rgb[:k, :k].mean(), rgb[:k, -k:].mean(),
        rgb[-k:, :k].mean(), rgb[-k:, -k:].mean(),
    ])
    return abs(corners - c) > 8 or corners < 40


def assess(path, forced):
    img = Image.open(path).convert("RGB")
    arr = np.asarray(img)                 # uint8
    rgb = arr.astype(np.float32)          # float for distance math (no overflow)
    h, w, _ = rgb.shape
    r = Result(str(path))
    round_disp = _is_round(arr, forced)
    r.metrics["shape"] = "round" if round_disp else "rect"

    cx, cy = w / 2.0, h / 2.0

    if round_disp:
        R = min(w, h) / 2.0
        yy, xx = np.mgrid[0:h, 0:w]
        radmap = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        # Erode the rim: the hardware clips outside the circle, and the mask edge
        # is antialiased, so analyse only well inside (<=0.97R). Background is the
        # modal colour INSIDE that disc — not the black corner mask, which would
        # otherwise read as "ink outside the circle".
        inside = radmap <= R * 0.97
        bg = _background(arr[inside]).astype(np.float32)
        dist = np.sqrt(((rgb - bg) ** 2).sum(axis=2))
        ink = (dist > INK_DIST) & inside
        ink_total = int(ink.sum())
        r.metrics["ink_px"] = ink_total
        if ink_total == 0:
            r.add("WARN", "blank", "no foreground ink detected")
            return r
        ys, xs = np.nonzero(ink)
        dx = xs - cx
        dy = ys - cy
        rad = np.sqrt(dx * dx + dy * dy)

        # CLIP: ink hugging the eroded rim, flagged only when concentrated in a
        # few arcs (a full wrap-around ring is decorative, not a clip).
        annulus = (rad >= R * EDGE_FRAC) & (rad <= R * 0.97)
        ann_n = int(annulus.sum())
        if ann_n > 0:
            ang = (np.degrees(np.arctan2(dy[annulus], dx[annulus])) + 360) % 360
            bins = np.zeros(ANGLE_BINS, dtype=int)
            for a in ang:
                bins[int(a // (360 / ANGLE_BINS)) % ANGLE_BINS] += 1
            coverage = (bins > 0).sum() / ANGLE_BINS
            r.metrics["rim_coverage"] = round(coverage, 2)
            if coverage >= RING_COVERAGE_OK:
                r.add("INFO", "edge_ring",
                      f"rim ink wraps {coverage*100:.0f}% — treated as decorative")
            elif ann_n > ink_total * 0.02:
                r.add("WARN", "edge_clip",
                      f"{ann_n} px of rim ink in {int(coverage*ANGLE_BINS)} arc(s) — near the bezel, check for clipping")
            elif ann_n > ink_total * 0.008:
                r.add("WARN", "edge_near",
                      f"{ann_n} px close to the bezel — check for clipping")

        # BAND: ink outside the inscribed safe square.
        outside_sq = (np.abs(dx) > R * INSCRIBED) | (np.abs(dy) > R * INSCRIBED)
        out_n = int(outside_sq.sum())
        frac = out_n / ink_total
        r.metrics["pct_outside_safe"] = round(frac * 100, 1)
        if frac > 0.10:
            r.add("WARN", "out_of_safe_band",
                  f"{frac*100:.0f}% of ink outside the round-safe inscribed square")
    else:
        bg = _background(arr).astype(np.float32)
        dist = np.sqrt(((rgb - bg) ** 2).sum(axis=2))
        ink = dist > INK_DIST
        ink_total = int(ink.sum())
        r.metrics["ink_px"] = ink_total
        if ink_total == 0:
            r.add("WARN", "blank", "no foreground ink detected")
            return r
        ys, xs = np.nonzero(ink)
        # Rectangular: flag ink in the outer 4% margin.
        mx, my = w * 0.04, h * 0.04
        edge = (xs < mx) | (xs > w - mx) | (ys < my) | (ys > h - my)
        edge_n = int(edge.sum())
        frac = edge_n / ink_total
        r.metrics["pct_in_margin"] = round(frac * 100, 1)
        if frac > 0.02:
            r.add("WARN", "margin_ink",
                  f"{frac*100:.1f}% of ink in the outer 4% margin — check for clipping")

    # Overlap (advisory): tall contiguous ink row-bands suggest merged lines.
    row_has = ink.any(axis=1)
    bands, start = [], None
    for i, v in enumerate(row_has):
        if v and start is None:
            start = i
        elif not v and start is not None:
            bands.append(i - start)
            start = None
    if start is not None:
        bands.append(len(row_has) - start)
    if len(bands) >= 5:
        med = sorted(bands)[len(bands) // 2]
        tall = [b for b in bands if b > med * 2.5 and b > h * 0.25]
        if tall:
            r.add("WARN", "tall_band",
                  f"{len(tall)} ink band(s) much taller than the median row — possible overlap")

    return r


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="ciqkit layout",
        description="Mechanical layout checks on captured watch screens.")
    ap.add_argument("paths", nargs="+",
                    help="native face PNGs, or directories of them")
    ap.add_argument("--round", dest="forced", action="store_const", const="round")
    ap.add_argument("--rect", dest="forced", action="store_const", const="rect")
    ap.add_argument("--fail-under", type=int, default=0,
                    help="exit 1 if any image scores below this")
    args = ap.parse_args(argv)

    SKIP = ("framed", "cover", "hero", "icon", "store-listing", "_debug", "debug-")
    files = []
    for p in args.paths:
        pp = Path(p)
        cand = sorted(pp.glob("*.png")) if pp.is_dir() else ([pp] if pp.suffix.lower() == ".png" else [])
        for c in cand:
            if any(s in c.name.lower() for s in SKIP):
                continue
            files.append(c)
    if not files:
        print("no native-face PNGs found", file=sys.stderr)
        return 2

    worst = 100
    err = False
    width = max(len(f.name) for f in files)
    for f in files:
        try:
            r = assess(f, args.forced)
        except Exception as e:
            print(f"  ERR  {f.name}: {e}")
            err = True
            continue
        worst = min(worst, r.score)
        tag = "WARN" if r.score < 88 else "OK"
        m = r.metrics
        msum = f"[{m.get('shape','?')}]"
        if "pct_outside_safe" in m:
            msum += f" safe-out={m['pct_outside_safe']}%"
        if "rim_coverage" in m:
            msum += f" rim={m['rim_coverage']}"
        print(f"  {tag:>4} {r.score:3}  {f.name:<{width}}  {msum}")
        for sev, code, detail in r.issues:
            print(f"            {sev:>4} {code}: {detail}")

    print(f"\nworst score: {worst}  (threshold --fail-under {args.fail_under})")
    if err or worst < args.fail_under:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
