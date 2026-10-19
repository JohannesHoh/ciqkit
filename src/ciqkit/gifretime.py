"""Re-time an already-written animated GIF so it actually animates.

`ciqkit sim record` used to store wall-clock frame durations, and back then a
grab cost ~2 s, so those GIFs replay at ~0.5 fps -- indistinguishable from a
still image in every viewer. `record` now time-compresses on write (and captures
at ~4.6 fps, so it rarely needs to); this module fixes the clips written before
that, in place, without re-running the simulator.

    python -m ciqkit.gifretime [--playback-ms 120] clip.gif [clip2.gif ...]
"""
from __future__ import annotations

import argparse
import sys

from ciqkit.sim.session import _compress_durations


def retime(path: str, playback_ms: int = 120) -> dict:
    """Rewrite `path` with time-compressed frame durations. Returns a summary."""
    from PIL import Image, ImageSequence

    with Image.open(path) as im:
        frames = [f.convert("RGB") for f in ImageSequence.Iterator(im)]
        wall = []
        for i, _f in enumerate(frames):
            im.seek(i)
            wall.append(max(20, int(im.info.get("duration", 100))))
    original = list(wall)
    if len(frames) < 2:
        return {"path": path, "frames": len(frames), "skipped": "single frame"}
    if len(wall) > 2:
        # Old clips stored the requested interval as the final frame's duration,
        # far below the real grab cost, so compression flashes it past. Give it
        # the median of the real ones instead.
        rest = sorted(wall[:-1])
        wall[-1] = rest[len(rest) // 2]
    durations, speedup = _compress_durations(wall, playback_ms)
    if durations == original:
        return {"path": path, "frames": len(frames), "skipped": "already fast enough"}
    frames[0].save(path, save_all=True, append_images=frames[1:],
                   duration=durations, loop=0, optimize=True)
    return {"path": path, "frames": len(frames), "speedup": round(speedup, 1),
            "gif_duration_s": round(sum(durations) / 1000.0, 2)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ciqkit.gifretime", description=__doc__)
    ap.add_argument("gifs", nargs="+")
    ap.add_argument("--playback-ms", type=int, default=120,
                    help="median frame duration after re-timing (default: 120)")
    a = ap.parse_args(argv)
    for g in a.gifs:
        print(retime(g, a.playback_ms))
    return 0


if __name__ == "__main__":
    sys.exit(main())
