"""The pre-load screen must never be mistaken for the app.

Connect IQ shows a blue triangle on black while an app is loading. That frame
is not blank, and it *has* changed from the watchface that was showing before,
so the two cheap load checks both pass on it. Without a dedicated test the
broker reports a successful load and captures a screenshot of the loader — the
confidently-wrong output the whole verify layer exists to prevent. This was a
real regression, caught end-to-end; `_loading_screen()` below draws a synthetic
stand-in for the frame that caught it (no Garmin screen is committed).

The opposite risk matters just as much: a detector tuned too loosely rejects a
legitimately dark app screen forever, and the load times out. Both directions
are pinned here.
"""
import pytest

pytest.importorskip("PIL", reason="Pillow not installed")
pytest.importorskip("numpy", reason="numpy not installed")

from PIL import Image, ImageDraw  # noqa: E402

from ciqkit.sim.session import SimSession  # noqa: E402


def _loading_screen():
    """Synthetic pre-load frame: a small blue triangle on an otherwise black face.

    Drawn, not captured, so no Garmin artwork is committed. The proportions are
    chosen to land inside the detector's real window (almost all dark, a blue
    mark of a few percent of the area, nothing else), so this exercises the
    thresholds rather than passing trivially.
    """
    img = Image.new("RGB", (160, 160), (0, 0, 0))
    ImageDraw.Draw(img).polygon([(80, 55), (58, 100), (102, 100)], fill=(20, 110, 230))
    return img


@pytest.fixture
def session():
    """A session that never touches a simulator, SDK or filesystem output."""
    s = SimSession.__new__(SimSession)
    s.current = None
    return s


def _no_face_crop(session, monkeypatch):
    """Force the detector to work on the whole image.

    Face detection needs the simulator's window geometry, which a synthetic
    fixture does not have.
    """
    monkeypatch.setattr(SimSession, "_crop_face", lambda self, img: None)


def test_loading_screen_is_rejected(session, monkeypatch):
    _no_face_crop(session, monkeypatch)
    img = _loading_screen()
    assert session._is_loading_screen(img) is True


def test_a_dark_app_screen_with_text_is_accepted(session, monkeypatch):
    """The realistic false-positive risk: a mostly-black screen with a label."""
    _no_face_crop(session, monkeypatch)
    img = Image.new("RGB", (200, 200), (0, 0, 0))
    d = ImageDraw.Draw(img)
    d.text((40, 90), "TOTAL 12", fill=(255, 255, 255))
    d.rectangle([20, 150, 180, 156], fill=(200, 200, 200))
    assert session._is_loading_screen(img) is False


def test_a_blue_themed_app_screen_is_accepted(session, monkeypatch):
    """Blue alone must not be the trigger — plenty of apps are blue."""
    _no_face_crop(session, monkeypatch)
    img = Image.new("RGB", (200, 200), (20, 60, 160))
    ImageDraw.Draw(img).text((40, 90), "SCORE", fill=(255, 255, 255))
    assert session._is_loading_screen(img) is False


def test_a_bright_screen_is_accepted(session, monkeypatch):
    _no_face_crop(session, monkeypatch)
    img = Image.new("RGB", (200, 200), (250, 250, 250))
    ImageDraw.Draw(img).rectangle([60, 60, 140, 140], fill=(0, 0, 0))
    assert session._is_loading_screen(img) is False


def test_an_all_black_screen_is_not_the_loader(session, monkeypatch):
    """No blue mark at all: blank, which `_is_blank` handles separately."""
    _no_face_crop(session, monkeypatch)
    img = Image.new("RGB", (200, 200), (0, 0, 0))
    assert session._is_loading_screen(img) is False


def test_the_loader_is_not_treated_as_an_app_frame(session, monkeypatch):
    """The integration point that actually gates a capture."""
    _no_face_crop(session, monkeypatch)
    monkeypatch.setattr(SimSession, "_face_sig", lambda self, img: (1, 2, 3, 4))
    img = _loading_screen()
    assert session._is_app_frame(img, before_sig=(9, 9, 9, 9)) is False


def test_the_detector_degrades_safely_on_a_broken_image(session, monkeypatch):
    """A detector that raises must not fail an otherwise good load."""
    _no_face_crop(session, monkeypatch)

    class Exploding:
        def convert(self, _mode):
            raise RuntimeError("boom")

    assert session._is_loading_screen(Exploding()) is False


def test_an_identical_rerun_is_accepted_after_the_loader(session, monkeypatch):
    """Re-running the same build shows a screen identical to the one already up.

    Requiring a frame difference would time out and burn a full simulator
    restart on every repeat capture. Having watched the loading screen come and
    go is proof enough that this frame is the freshly loaded app.
    """
    _no_face_crop(session, monkeypatch)
    same_sig = (5, 5, 5, 5)
    monkeypatch.setattr(SimSession, "_face_sig", lambda self, img: same_sig)
    img = Image.new("RGB", (200, 200), (0, 0, 0))
    ImageDraw.Draw(img).text((40, 90), "TOTAL 12", fill=(255, 255, 255))

    assert session._is_app_frame(img, before_sig=same_sig, saw_loader=False) is False
    assert session._is_app_frame(img, before_sig=same_sig, saw_loader=True) is True


def test_seeing_the_loader_does_not_excuse_a_blank_screen(session, monkeypatch):
    _no_face_crop(session, monkeypatch)
    blank = Image.new("RGB", (200, 200), (0, 0, 0))
    assert session._is_app_frame(blank, before_sig=None, saw_loader=True) is False
