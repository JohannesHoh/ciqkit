"""Direct tests of the pure frame predicates, on small synthetic images."""
import pytest

pytest.importorskip("PIL", reason="Pillow not installed")
pytest.importorskip("numpy", reason="numpy not installed")

from PIL import Image, ImageDraw  # noqa: E402

from ciqkit.capture import verify  # noqa: E402


def _flat(color=(0, 0, 0), size=(120, 120)):
    return Image.new("RGB", size, color)


def _busy(size=(120, 120)):
    img = _flat((0, 0, 0), size)
    d = ImageDraw.Draw(img)
    for i in range(0, size[0], 12):
        d.rectangle([i, 0, i + 5, size[1]], fill=(255, 255, 255))
    return img


def _circle(img):
    w, h = img.size
    return w // 2, h // 2, min(w, h) // 2


def _sig(img):
    return verify.face_sig(img, _circle(img))


def test_blank_frame_is_blank():
    assert verify.is_blank(_flat((10, 10, 10))) is True


def test_textured_frame_is_not_blank():
    assert verify.is_blank(_busy()) is False


def test_unchanged_pair_is_not_a_change():
    a, b = _busy(), _busy()
    assert verify.input_changed(a, b, _sig, verify.frame_detail) is False


def test_changed_pair_is_a_change():
    a = _busy()
    b = _flat((255, 255, 255))
    assert verify.input_changed(a, b, _sig, verify.frame_detail) is True


def test_face_circle_falls_back_to_centre_on_declared_circle_none(monkeypatch):
    monkeypatch.setattr(verify.su, "find_watch_face_auto", lambda *a, **k: None)
    assert verify.face_circle(_flat(size=(100, 80)), 454, None) == (50, 40, 20)
    assert verify.face_circle(_flat(), 454, (1, 2, 3)) == (1, 2, 3)


# --- pixels_changed: the strict check act() makes before any re-send ---------

def _hud(digit_on=False, size=(240, 240)):
    """A dark HUD with a grey label; ``digit_on`` lights one short segment of a
    small digit (a 1 -> 7 style change, 3x2 = 6 px) -- the whole effect of a
    BACK press that bumps a counter."""
    img = _flat((0, 0, 0), size)
    d = ImageDraw.Draw(img)
    d.rectangle([60, 100, 180, 112], fill=(90, 90, 90))      # static label
    d.rectangle([120, 150, 121, 160], fill=(200, 200, 200))  # the "1"
    if digit_on:
        d.rectangle([117, 150, 119, 151], fill=(200, 200, 200))
    return img


def _noisy(img, amp=2, seed=0):
    import numpy as np
    rng = np.random.default_rng(seed)
    arr = np.asarray(img, dtype=np.int16)
    arr = arr + rng.integers(-amp, amp + 1, size=arr.shape)
    return Image.fromarray(np.clip(arr, 0, 255).astype("uint8"), "RGB")


def test_a_one_digit_change_is_a_change_the_coarse_test_misses():
    a, b = _hud(), _hud(digit_on=True)
    # The gap this check closes: the coarse test reads it as nothing.
    assert verify.input_changed(a, b, _sig, verify.frame_detail) is False
    assert verify.pixels_changed(a, b) is True


def test_an_identical_frame_is_not_a_change():
    assert verify.pixels_changed(_hud(), _hud()) is False


def test_capture_noise_everywhere_is_not_a_change():
    a = _noisy(_hud(), seed=1)
    b = _noisy(_hud(), seed=2)
    assert verify.pixels_changed(a, b) is False


def test_a_digit_change_under_noise_is_still_a_change():
    assert verify.pixels_changed(_noisy(_hud(), seed=1),
                                 _noisy(_hud(digit_on=True), seed=2)) is True


def test_one_stray_pixel_is_not_a_change():
    a, b = _hud(), _hud()
    b.putpixel((10, 10), (255, 255, 255))
    assert verify.pixels_changed(a, b) is False


def test_the_box_limits_the_comparison_to_the_display():
    a, b = _hud(), _hud(digit_on=True)
    assert verify.pixels_changed(a, b, box=(0, 0, 100, 100)) is False
    assert verify.pixels_changed(a, b, box=(100, 140, 140, 170)) is True


def test_frames_of_different_sizes_prove_no_change():
    assert verify.pixels_changed(_hud(), _hud(size=(240, 200))) is False


def test_pixels_that_moved_before_the_input_are_ignored():
    base = _hud()
    probe = _hud()
    ImageDraw.Draw(probe).rectangle([200, 20, 203, 23], fill=(255, 255, 255))
    mask = verify.unstable_mask(base, probe)
    after = _hud()
    ImageDraw.Draw(after).rectangle([200, 20, 203, 23], fill=(255, 255, 255))
    assert verify.pixels_changed(base, after) is True
    assert verify.pixels_changed(base, after, ignore=mask) is False
    # A real change elsewhere still counts.
    ImageDraw.Draw(after).rectangle([117, 150, 119, 151], fill=(200, 200, 200))
    assert verify.pixels_changed(base, after, ignore=mask) is True
