"""`ciqkit layout` -- geometry checks on a round watch face.

The value of this check is entirely in its false-alarm rate. A clean screen
must score 100, or the score stops meaning anything and gets ignored; a label
running off the bezel must lose points, or it catches nothing. Both directions
are pinned, including the decorative-ring case that a naive edge-ink check
would flag every time.
"""
import pytest

pytest.importorskip("PIL", reason="Pillow not installed")
pytest.importorskip("numpy", reason="numpy not installed")

from PIL import Image, ImageDraw  # noqa: E402

from ciqkit.qa import layout  # noqa: E402

SIZE = 454
R = SIZE // 2


def _face(draw_fn=None):
    """A round watch face: black outside the circle, dark inside."""
    img = Image.new("RGB", (SIZE, SIZE), (0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([0, 0, SIZE - 1, SIZE - 1], fill=(10, 10, 10))
    if draw_fn:
        draw_fn(d)
    return img


def _run(tmp_path, img, *args, name="s.png"):
    p = tmp_path / name
    img.save(p)
    return layout.main([str(p), *args])


# --- the tool runs and stays advisory ---------------------------------------

def test_a_clean_centred_screen_runs_and_passes(tmp_path, capsys):
    def content(d):
        d.text((R - 40, R - 40), "TOTAL", fill=(255, 255, 255))
        d.rectangle([R - 80, R + 10, R + 80, R + 18], fill=(200, 200, 200))
    assert _run(tmp_path, _face(content)) == 0
    out = capsys.readouterr().out
    assert "100" in out


def test_it_is_advisory_unless_you_ask_for_a_gate(tmp_path):
    """Content jammed into the bezel: still exit 0 without --fail-under."""
    def clipped(d):
        d.rectangle([2, R - 8, SIZE - 3, R + 8], fill=(255, 255, 255))
    assert _run(tmp_path, _face(clipped)) == 0


def test_fail_under_turns_a_low_score_into_a_nonzero_exit(tmp_path):
    def clipped(d):
        d.rectangle([2, R - 8, SIZE - 3, R + 8], fill=(255, 255, 255))
    assert _run(tmp_path, _face(clipped), "--fail-under", "100") == 1


def test_a_clean_screen_passes_its_own_gate(tmp_path):
    def content(d):
        d.text((R - 40, R - 40), "TOTAL", fill=(255, 255, 255))
    assert _run(tmp_path, _face(content), "--fail-under", "90") == 0


# --- the false-alarm case that makes or breaks the check --------------------

def test_a_decorative_full_ring_is_not_reported_as_a_clip(tmp_path):
    """A progress arc drawn near the rim is intentional. Flagging it would make
    every watch face that has one score badly."""
    def ring(d):
        d.ellipse([6, 6, SIZE - 7, SIZE - 7], outline=(90, 160, 240), width=7)
        d.text((R - 30, R - 10), "42", fill=(255, 255, 255))
    assert _run(tmp_path, _face(ring), "--fail-under", "80") == 0


# --- input selection --------------------------------------------------------

def test_non_native_assets_are_skipped_by_filename(tmp_path, capsys):
    """Framed/cover/hero/icon assets have an inset face, so the geometry does
    not apply. The filter is by NAME, which is worth knowing when yours differ.
    """
    _face().save(tmp_path / "shot-framed.png")
    rc = layout.main([str(tmp_path)])
    assert rc == 2
    assert "no native-face PNGs" in capsys.readouterr().err


def test_a_directory_of_faces_is_scanned(tmp_path, capsys):
    for n in ("a.png", "b.png"):
        _face().save(tmp_path / n)
    assert layout.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "a.png" in out and "b.png" in out


def test_geometry_can_be_forced(tmp_path):
    """A rectangular screen has no dead corners, so the round rules must not
    be applied to it."""
    img = Image.new("RGB", (SIZE, SIZE), (10, 10, 10))
    ImageDraw.Draw(img).rectangle([2, 2, SIZE - 3, 40], fill=(255, 255, 255))
    assert _run(tmp_path, img, "--rect", name="rect.png") == 0
