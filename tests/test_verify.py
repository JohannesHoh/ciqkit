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
