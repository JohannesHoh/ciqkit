"""Launcher-icon checks and beta-ribbon compositing.

Two things that are easy to get wrong and invisible until users see them:

``beta_ribbon``
    Overlay a diagonal BETA banner on a launcher icon, so testers can tell a
    beta install apart from the released app on a crowded watch launcher.
    Composite at *build* time onto a clean icon -- never commit an icon that
    already has a ribbon, or beta builds end up with two.

``find_duplicates``
    Refuse to ship an icon that is byte-identical to another project's. New
    projects are often bootstrapped by copying a working icon so the compiler
    has a decodable PNG; that placeholder is invisible in day-to-day work and
    then ships. Two of your own apps become indistinguishable in the launcher
    and on their store pages.

CLI::

    ciqkit icon beta   ICON.png OUT.png
    ciqkit icon unique PROJECT_DIR [OTHER_PROJECT_DIR ...]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

# Where the Connect IQ resource compiler expects a project's launcher icon.
ICON_REL_PATH = os.path.join("resources", "drawables", "launcher_icon.png")

BANNER_COLOR = (220, 30, 30, 235)          # red with slight transparency
BANNER_TEXT_COLOR = (255, 255, 255, 255)
BANNER_TEXT = "BETA"

_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)


def _require_pillow():
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "Pillow is required for icon compositing. Install it with:\n"
            "    pip install 'ciqkit[checks]'   (or: pip install Pillow)") from exc
    return Image, ImageDraw, ImageFont


def _pick_font(target_px: int):
    _Image, _Draw, ImageFont = _require_pillow()
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, target_px)
            except OSError:
                continue
    # PIL's built-in bitmap font ignores the size, so the ribbon text will be
    # small on large icons -- readable, just not scaled.
    return ImageFont.load_default()


def beta_ribbon(src: str | Path, dst: str | Path) -> Path:
    """Write ``src`` to ``dst`` with a diagonal BETA banner across the corner.

    Source dimensions are preserved. The banner scales to ~30% of the icon
    width, so it stays readable from a 40x40 launcher tile up to a 512x512
    store asset.
    """
    Image, ImageDraw, _Font = _require_pillow()
    src, dst = Path(src), Path(dst)
    if not src.is_file():
        raise FileNotFoundError(f"source icon not found: {src}")

    base = Image.open(src).convert("RGBA")
    W, H = base.size

    band_h = max(int(W * 0.30), 9)
    font = _pick_font(max(int(band_h * 0.62), 7))

    band = Image.new("RGBA", (W * 2, band_h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(band)
    bd.rectangle([0, 0, band.width, band_h], fill=BANNER_COLOR)

    bbox = bd.textbbox((0, 0), BANNER_TEXT, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    bd.text(((band.width - tw) // 2, (band_h - th) // 2 - 1),
            BANNER_TEXT, fill=BANNER_TEXT_COLOR, font=font)

    rotated = band.rotate(45, resample=Image.BICUBIC, expand=True)
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    overlay.paste(rotated,
                  (-rotated.width // 2 + int(W * 0.20),
                   -rotated.height // 2 + int(H * 0.20)),
                  rotated)

    dst.parent.mkdir(parents=True, exist_ok=True)
    Image.alpha_composite(base, overlay).save(dst, "PNG")
    return dst


def _sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def icon_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / ICON_REL_PATH


def find_duplicates(project_dir: str | Path,
                    others: list[str | Path]) -> list[Path]:
    """Return the projects in ``others`` whose launcher icon is byte-identical.

    An empty list means the icon is this project's own. A missing icon in
    either project is not an error here -- a project without an icon fails the
    build for its own reasons.
    """
    mine = icon_path(project_dir)
    if not mine.is_file():
        return []
    digest = _sha256(mine)
    hits = []
    for other in others:
        other_icon = icon_path(other)
        if other_icon.resolve() == mine.resolve():
            continue
        if other_icon.is_file() and _sha256(other_icon) == digest:
            hits.append(Path(other))
    return hits


def _expand_peers(paths: list[str]) -> list[str]:
    """Accept either project dirs or a parent dir holding several projects."""
    out: list[str] = []
    for p in paths:
        if icon_path(p).is_file():
            out.append(p)
            continue
        if os.path.isdir(p):
            out.extend(os.path.join(p, d) for d in sorted(os.listdir(p))
                       if icon_path(os.path.join(p, d)).is_file())
    return out


def build_parser(prog="ciqkit icon"):
    p = argparse.ArgumentParser(prog=prog, description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("beta", help="composite a BETA ribbon onto an icon")
    sp.add_argument("src", help="clean source icon PNG")
    sp.add_argument("dst", help="output PNG (overwritten)")

    sp = sub.add_parser(
        "unique",
        help="fail if this project's launcher icon is copied from another")
    sp.add_argument("project", help="the Connect IQ project directory to check")
    sp.add_argument("others", nargs="*",
                    help="other project directories, or one parent directory "
                         "containing them. Default: the project's siblings.")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "beta":
        out = beta_ribbon(args.src, args.dst)
        print(f"wrote {out}")
        return 0

    project = os.path.abspath(args.project)
    peers = _expand_peers(args.others) if args.others else _expand_peers(
        [os.path.dirname(project)])
    if not peers:
        print("no other projects with a launcher icon to compare against; "
              "nothing to check")
        return 0

    clashes = find_duplicates(project, peers)
    if not clashes:
        return 0

    names = ", ".join(os.path.basename(os.path.normpath(c)) for c in clashes)
    print(
        f"\nERROR: {os.path.join(os.path.basename(project), ICON_REL_PATH)} is "
        f"byte-identical to: {names}\n\n"
        f"  That icon is a placeholder copied from another project, not this\n"
        f"  app's own. Draw a real 70x70 RGBA icon before shipping.\n\n"
        f"  Tips: render at ~8x and downsample with LANCZOS -- 70px is\n"
        f"  unforgiving of thin strokes. Then look at it at TRUE size, and at\n"
        f"  the beta composite testers will see:\n"
        f"    ciqkit icon beta {os.path.join(project, ICON_REL_PATH)} /tmp/beta.png\n",
        file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
