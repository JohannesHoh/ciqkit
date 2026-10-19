"""Redraw docs/demo/ciqkit-demo.gif, the README's first picture.

Two steps, so the slow one can be skipped:

    # 1. capture: drives the real simulator through the broker (~4 min, macOS)
    python docs/demo/make_demo.py capture --work /tmp/ciqkit-demo

    # 2. render: turns that run's own output into the GIF (no simulator needed)
    python docs/demo/make_demo.py render --work /tmp/ciqkit-demo

Nothing in the GIF is typed by hand: the terminal lines are the JSON that
`ciqkit sim shots` printed and the scores `ciqkit layout` printed, and the
watch is the PNG each job captured, from examples/tictactoe.

Captures use --native, but the simulator still paints its device bezel into
the corners of the square grab. That bezel is Garmin's artwork, so render
masks every frame to the display circle: only the example app's own pixels
end up in the committed GIF.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROJECT = REPO / "examples" / "tictactoe"
OUT = REPO / "docs" / "demo" / "ciqkit-demo.gif"
DEVICE = "venu3"
SHOTS_CMD = "ciqkit sim shots --all --native"
LAYOUT_CMD = "ciqkit layout shots/"


def _ciqkit() -> str:
    # Prefer the repo's own venv so the demo shows this checkout, not whatever
    # ciqkit happens to be first on PATH.
    local = REPO / ".venv" / "bin" / "ciqkit"
    return str(local) if local.exists() else "ciqkit"


def capture(work: Path) -> None:
    shots = work / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    run = subprocess.run(
        [_ciqkit(), "sim", "shots", "--all", "--native", "--device", DEVICE,
         "--project", str(PROJECT), "--out-dir", str(shots)],
        capture_output=True, text=True,
    )
    (work / "shots.jsonl").write_text(run.stdout)
    if run.returncode != 0:
        sys.exit(f"shots failed (exit {run.returncode}):\n{run.stdout}\n{run.stderr}")
    layout = subprocess.run([_ciqkit(), "layout", str(shots)], capture_output=True, text=True)
    (work / "layout.txt").write_text(layout.stdout)
    print(f"captured into {work}; now: python {Path(__file__).name} render --work {work}")


def _font(size: int):
    from PIL import ImageFont

    for path in ("/System/Library/Fonts/Menlo.ttc",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _watch(png: Path, size: int):
    from PIL import Image, ImageDraw

    im = Image.open(png).convert("RGB").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    inset = round(size * 0.035)  # just inside the bezel ring
    ImageDraw.Draw(mask).ellipse((inset, inset, size - inset, size - inset), fill=255)
    out = Image.new("RGB", (size, size), BG)
    out.paste(im, (0, 0), mask)
    ImageDraw.Draw(out).ellipse((inset - 3, inset - 3, size - inset + 3, size - inset + 3),
                                outline=(70, 74, 82), width=4)
    return out


BG = (22, 24, 29)
FG = (215, 218, 224)
DIM = (120, 126, 138)
GREEN = (96, 200, 120)
AMBER = (230, 180, 80)
PROMPT = (110, 160, 240)


def render(work: Path, out: Path) -> None:
    from PIL import Image, ImageDraw

    results = [json.loads(line) for line in (work / "shots.jsonl").read_text().splitlines()
               if line.strip()]
    anchors = {s["name"]: s.get("anchor") for s in
               tomllib.loads((PROJECT / "ciqkit.toml").read_text("utf-8"))["screens"]}
    layout = [ln.strip() for ln in (work / "layout.txt").read_text().splitlines()
              if ln.strip().startswith("OK") or ln.strip().startswith("FAIL")]

    W, H, WS = 960, 400, 340
    font, small = _font(17), _font(14)
    lines: list[tuple[str, tuple]] = []
    frames, durations = [], []
    current = None

    def snap(ms: int) -> None:
        im = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(im)
        d.text((24, 16), "examples/tictactoe  ·  venu3  ·  real simulator", fill=DIM, font=small)
        y = 50
        for text, color in lines[-14:]:
            d.text((24, y), text, fill=color, font=font)
            y += 24
        if current is not None:
            im.paste(current, (W - WS - 24, (H - WS) // 2))
        frames.append(im)
        durations.append(ms)

    cmd = f"$ {SHOTS_CMD}"
    for cut in (len(cmd) // 3, 2 * len(cmd) // 3, len(cmd)):
        lines[:] = [(cmd[:cut], PROMPT)]
        snap(220)
    snap(500)
    for r in results:
        name = r["screen"]
        if not r.get("ok"):
            lines.append((f"FAIL {name}: {r.get('error', '')[:40]}", AMBER))
            snap(1500)
            continue
        current = _watch(Path(r["path"]), WS)
        lines.append((f"ok  {name:<9} loaded, verified", GREEN))
        if anchors.get(name):
            seen = "seen" if r.get("anchor") else "not confirmed"
            lines.append((f"    text \"{anchors[name]}\" {seen}", FG))
        snap(1500)
    lines.append(("", FG))
    lines.append((f"$ {LAYOUT_CMD}", PROMPT))
    snap(600)
    for ln in layout:
        parts = ln.split()
        # "OK  88  draw.png  [round] safe-out=12.9%" -> keep score + file + note
        score, fname = parts[1], parts[2].removesuffix(".png")
        note = "warn: ink near rim" if score != "100" else ""
        lines.append((f"{score:>4}  {fname:<9} {note}", GREEN if score == "100" else AMBER))
    snap(3500)

    out.parent.mkdir(parents=True, exist_ok=True)
    pal = [f.convert("P", palette=Image.ADAPTIVE, colors=96) for f in frames]
    pal[0].save(out, save_all=True, append_images=pal[1:], duration=durations, loop=0,
                optimize=True)
    print(f"wrote {out} ({out.stat().st_size // 1024} KB, {len(frames)} frames)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("step", choices=["capture", "render", "all"])
    ap.add_argument("--work", type=Path, required=True, help="scratch dir for the captured run")
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()
    if a.step in ("capture", "all"):
        capture(a.work)
    if a.step in ("render", "all"):
        render(a.work, a.out)


if __name__ == "__main__":
    main()
