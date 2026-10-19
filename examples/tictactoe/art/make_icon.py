"""Draw the launcher icons.

    resources/drawables/launcher_icon.png          70x70 (venu3)
    resources-fr255/drawables/launcher_icon.png    40x40
    resources-venusq2/drawables/launcher_icon.png  40x40

A `resources-<device id>` directory overrides `resources/` for that device, so
each device gets an icon at its own size instead of a compiler-scaled one. When
you add a device, check its launcher icon size (the compiler warns when it has
to scale) and add a directory here if it differs.

Original artwork for this example (a 3x3 grid with an X, an O and a winning
stroke). Drawn at 8x and downsampled for smooth edges.

    python3 art/make_icon.py          # needs Pillow
"""

from pathlib import Path

from PIL import Image, ImageDraw

TARGETS = {
    "resources": 70,
    "resources-fr255": 40,
    "resources-venusq2": 40,
}
SCALE = 8
BLUE = (0, 170, 255, 255)
ORANGE = (255, 85, 0, 255)
GRID = (200, 200, 200, 255)
DISC = (24, 24, 24, 255)


def draw(size: int) -> Image.Image:
    s = size * SCALE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, s - 1, s - 1), fill=DISC)

    # Grid inside the disc's inscribed square, with a little margin.
    g0, g1 = int(s * 0.22), int(s * 0.78)
    cell = (g1 - g0) / 3
    w = int(s * 0.035)
    for k in (1, 2):
        x = g0 + k * cell
        d.line((x, g0, x, g1), fill=GRID, width=w)
        d.line((g0, x, g1, x), fill=GRID, width=w)

    def centre(i: int) -> tuple[float, float]:
        return g0 + (i % 3 + 0.5) * cell, g0 + (i // 3 + 0.5) * cell

    pad = cell * 0.27
    mark = int(s * 0.05)
    for i in (0, 4, 8):                      # X on the diagonal
        cx, cy = centre(i)
        r = cell / 2 - pad
        d.line((cx - r, cy - r, cx + r, cy + r), fill=BLUE, width=mark)
        d.line((cx - r, cy + r, cx + r, cy - r), fill=BLUE, width=mark)
    for i in (2, 6):                         # O in two corners
        cx, cy = centre(i)
        r = cell / 2 - pad
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=ORANGE, width=mark)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    for res_dir, size in TARGETS.items():
        out = root / res_dir / "drawables" / "launcher_icon.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        draw(size).save(out)
        print(out)


if __name__ == "__main__":
    main()
