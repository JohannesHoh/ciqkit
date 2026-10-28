"""Pure helpers behind :meth:`SimSession.record` and ``run_tests``.

Recording timing (turning capture timestamps into a GIF track that animates),
the contact sheet, and the test-harness output parser. Nothing here captures a
frame or sends input -- that stays in :mod:`ciqkit.sim.session` -- so all of it
runs headless. Stdlib-only at import; Pillow is imported where it is used.
"""
from __future__ import annotations

import re


def gif_durations(stamps: list[float], overhead_gaps: set[int], interval_ms: int,
                  playback_ms: int) -> tuple[list[int], float]:
    """Per-frame GIF durations (ms) and the playback speed-up, from the capture
    time of each frame. ``overhead_gaps`` indexes the deltas that measure
    ciqkit's own bookkeeping (see :func:`_flatten_overhead_gaps`)."""
    wall = []
    for i in range(len(stamps) - 1):
        wall.append(max(20, int((stamps[i + 1] - stamps[i]) * 1000)))
    # Flatten BEFORE extending the track, so the final frame cannot inherit
    # a raw overhead gap: overhead_gaps indexes real deltas, and a tail that
    # copied wall[-1] first would keep the un-flattened value if that last
    # delta happened to be a refocus (it is, whenever the clip ends just
    # after one) -- which is exactly how a 1.4 s freeze survived on the
    # final frame of a measured clip.
    wall = _flatten_overhead_gaps(wall, overhead_gaps)
    # The last frame has no successor to time it: reuse the previous real
    # delta rather than the requested interval, which is a floor the grab
    # never reaches and would flash the final frame past in a few ms.
    wall.append(wall[-1] if wall else interval_ms)
    return _compress_durations(wall, playback_ms)


def _contact_sheet(imgs, path: str, cols: int = 6, tile_w: int = 227):
    """Grid montage of every frame (row-major, numbered), for frame-by-frame
    review. Tiles are downscaled to tile_w so a 40-frame clip stays a
    readable, reasonably-sized single PNG."""
    from PIL import Image, ImageDraw
    if not imgs:
        return
    # Never pad out to the full grid width: captures are slow, so a short
    # clip is often 1-2 frames, and a fixed 6-wide grid renders those as one
    # thumbnail beside five empty cells that look like failed captures.
    cols = max(1, min(cols, len(imgs)))
    w, h = imgs[0].size
    scale = tile_w / float(w)
    tw, th = tile_w, max(1, int(h * scale))
    rows = (len(imgs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw, rows * th), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    for i, im in enumerate(imgs):
        tile = im.resize((tw, th))
        x, y = (i % cols) * tw, (i // cols) * th
        sheet.paste(tile, (x, y))
        draw.text((x + 4, y + 2), str(i), fill=(255, 235, 59))
    sheet.save(path)


def _flatten_overhead_gaps(wall_ms: list[int], overhead: set[int]) -> list[int]:
    """Replace frame durations that measure ciqkit, not the app.

    Frame durations come from the real capture timestamps so that a genuine
    pause on the watch still reads as a pause. But two of the gaps in a clip
    are not the watch pausing -- they are this module doing bookkeeping between
    two grabs: the ~0.5 s canvas-mask build after frame 0, and the ~1.05 s
    window re-raise before every `refocus_every`-th frame.

    At the old ~0.5 fps those hid inside a two-second frame time. At ~4.6 fps
    they do not: measured on a real clip, the refocus frame held for 1450 ms
    against a 170 ms median -- a 8x freeze punched into the playback every
    ~5 s, in a clip whose whole point is watching motion. `_compress_durations`
    cannot help, because a healthy clip is already faster than `playback_ms`
    and passes through untouched, and its [20, 1000] ms clamp only applies on
    the branch that scales.

    So flatten these gaps to the median of the honest ones. That is not a lie
    about the app: nothing happened on the watch during a window raise that the
    neighbouring frames do not already show. Real stalls -- a slow redraw, a
    blocking dialog -- are untouched and still read as stalls.
    """
    honest = [d for i, d in enumerate(wall_ms) if i not in overhead]
    if not honest or len(honest) == len(wall_ms):
        return list(wall_ms)
    ordered = sorted(honest)
    mid = len(ordered) // 2
    median = int(ordered[mid] if len(ordered) % 2
                 else (ordered[mid - 1] + ordered[mid]) / 2.0)
    # Only ever SHORTEN an overhead gap. If a gap we flagged came in under the
    # median anyway, the bookkeeping evidently cost nothing and stretching it
    # out to the median would invent a pause that never happened.
    return [min(d, median) if i in overhead else d
            for i, d in enumerate(wall_ms)]


def _compress_durations(wall_ms: list[int], playback_ms: int) -> tuple[list[int], float]:
    """Scale wall-clock frame durations into a playable GIF timing track.

    Keeps the RATIOS between frames (so a real pause still reads as a pause) but
    scales the whole track so the MEDIAN frame lasts `playback_ms`. Median, not
    mean, so one slow grab does not drag every other frame down with it.
    `playback_ms <= 0` returns the wall-clock track untouched.

    Frames are clamped to [20, 1000] ms: under 20 ms most viewers silently
    substitute 100 ms, and a single outlier over 1 s looks like the clip froze.
    """
    if playback_ms <= 0 or not wall_ms:
        return list(wall_ms), 1.0
    ordered = sorted(wall_ms)
    mid = len(ordered) // 2
    median = (ordered[mid] if len(ordered) % 2
              else (ordered[mid - 1] + ordered[mid]) / 2.0)
    if median <= 0:
        return list(wall_ms), 1.0
    scale = playback_ms / float(median)
    if scale >= 1.0:
        # Already at or slower than the requested pace -- do not stretch a clip
        # that plays fine, only compress one that does not.
        return list(wall_ms), 1.0
    return [max(20, min(1000, int(round(d * scale)))) for d in wall_ms], 1.0 / scale


_VERDICT = re.compile(r"^(PASSED|FAILED)\s*\(passed=(\d+),\s*failed=(\d+)\s*,\s*"
                      r"errors=(\d+)\)", re.M)
_LEGACY = re.compile(r"Passed[:\s]+(\d+).*?Failed[:\s]+(\d+)", re.S | re.I)
_RAN = re.compile(r"^Ran (\d+) tests?$", re.M)


def _parse_test_output(out: str) -> dict:
    """Harness text -> {ok, passed, failed, errors, ran, reason?, out}."""
    tail = out[-2000:]
    m = _VERDICT.search(out)
    if m:
        verdict = m.group(1)
        passed, failed, errors = int(m.group(2)), int(m.group(3)), int(m.group(4))
    else:
        legacy = _LEGACY.search(out)
        if not legacy:
            reason = ("simulator-unreachable" if "Unable to connect to simulator" in out
                      else "no-verdict")
            return {"ok": False, "reason": reason, "passed": 0, "failed": 0, "out": tail}
        passed, failed, errors = int(legacy.group(1)), int(legacy.group(2)), 0
        verdict = "PASSED" if failed == 0 else "FAILED"
    ran_m = _RAN.search(out)
    ran = int(ran_m.group(1)) if ran_m else passed + failed + errors
    res = {"ok": verdict == "PASSED" and failed == 0 and errors == 0 and ran > 0,
           "passed": passed, "failed": failed, "errors": errors, "ran": ran, "out": tail}
    if ran == 0:
        res["reason"] = "zero-tests"
    return res
