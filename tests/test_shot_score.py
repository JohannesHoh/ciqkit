"""Screenshot scoring must not cry wolf on its own output.

Every finding here was a false positive produced by running `shot-score`
against screenshots `ciqkit sim shot` had just captured on a Retina Mac. A
check that fails good images gets ignored, and then it protects nothing — so
both directions are pinned: correct captures score clean, broken ones do not.
"""
import pytest

pytest.importorskip("PIL", reason="Pillow not installed")
pytest.importorskip("numpy", reason="numpy not installed")

from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

from ciqkit.qa import shot_score  # noqa: E402


def _watch_face(size, blur=0.0):
    """A plausible dark round watch screen with some bright content on it."""
    img = Image.new("RGB", (size, size), (0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([0, 0, size - 1, size - 1], fill=(8, 8, 8))
    step = max(2, size // 40)
    for i in range(6):
        y = int(size * (0.25 + i * 0.09))
        d.rectangle([size // 4, y, size * 3 // 4, y + step], fill=(235, 235, 235))
    d.ellipse([size // 2 - step * 4, size // 2 - step * 4,
               size // 2 + step * 4, size // 2 + step * 4], fill=(60, 140, 240))
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    return img


def _codes(result):
    return {f.code for f in result.findings}


# --- HiDPI capture sizes ----------------------------------------------------

def test_a_1x_native_size_is_recognised():
    kind, label, scale = shot_score._classify_size(454, 454, [])
    assert (kind, scale) == ("native_face", 1)
    assert label == "venu3_native"


@pytest.mark.parametrize("scale", [2, 3])
def test_a_hidpi_capture_of_a_native_size_is_recognised(scale):
    """A Retina Mac hands back 908x908 for a 454x454 face. That is correct."""
    kind, label, got = shot_score._classify_size(454 * scale, 454 * scale, [])
    assert (kind, got) == ("native_face", scale)
    assert label == f"venu3_native@{scale}x"


def test_a_genuinely_odd_size_is_still_unknown():
    assert shot_score._classify_size(777, 640, []) == (None, None, 0)


def test_a_hidpi_capture_does_not_warn_about_its_size(tmp_path):
    p = tmp_path / "shot.png"
    _watch_face(908).save(p)
    assert "unknown_size" not in _codes(shot_score.assess(str(p)))


# --- sharpness is measured at logical scale ---------------------------------

def test_a_sharp_hidpi_capture_is_not_called_blurry(tmp_path):
    """Upscaling adds pixels but no detail, which deflates Laplacian variance.

    Measuring the raw 2x image reported a perfectly sharp capture as blurry.
    """
    p = tmp_path / "sharp.png"
    _watch_face(908).save(p)
    assert "blurry" not in _codes(shot_score.assess(str(p)))


def test_a_genuinely_blurred_capture_is_still_caught(tmp_path):
    p = tmp_path / "blurred.png"
    _watch_face(908, blur=8).save(p)
    assert "blurry" in _codes(shot_score.assess(str(p)))


def test_logical_downscale_only_applies_above_1x():
    import numpy as np
    a = np.arange(36, dtype=np.uint8).reshape(6, 6)
    assert shot_score._to_logical_scale(a, 1).shape == (6, 6)
    assert shot_score._to_logical_scale(a, 2).shape == (3, 3)


# --- the loading screen ------------------------------------------------------

def test_the_connect_iq_loading_screen_scores_badly(tmp_path):
    """The capture-time failure this whole check exists for."""
    p = tmp_path / "loading.png"
    img = Image.new("RGB", (454, 454), (0, 0, 0))
    ImageDraw.Draw(img).polygon([(227, 190), (270, 264), (184, 264)],
                                fill=(41, 128, 200))
    img.save(p)
    r = shot_score.assess(p)
    assert "looks_like_loading_screen" in _codes(r)
    assert r.score < 100


# --- the gate must not pass by finding nothing to check ----------------------

def test_pointing_the_gate_at_nothing_is_an_error_not_a_pass(tmp_path, capsys):
    """Used as a CI quality gate, a renamed output directory would otherwise
    score nothing, find nothing below the threshold, and report a clean pass."""
    assert shot_score.main([str(tmp_path), "--fail-under", "70"]) == 2
    assert "no PNG/JPG files found" in capsys.readouterr().err


def test_a_mistyped_path_is_reported_rather_than_crashing(tmp_path, capsys):
    """It used to be handed straight to `assess`, which died on it."""
    good = tmp_path / "home.png"
    _watch_face(454).save(good)
    rc = shot_score.main([str(good), str(tmp_path / "typo.png")])
    out = capsys.readouterr()
    assert rc == 2, "a run that could not check what it was asked must not pass"
    assert "no such file or directory" in out.err
    assert "typo.png" in out.err


def test_json_output_stays_parseable_when_there_is_nothing_to_score(tmp_path,
                                                                    capsys):
    """Diagnostics belong on stderr: stdout carries the JSON a script parses."""
    import json
    assert shot_score.main([str(tmp_path), "--json"]) == 2
    assert json.loads(capsys.readouterr().out) == []


def test_a_real_directory_still_scores_and_passes(tmp_path, capsys):
    _watch_face(454).save(tmp_path / "home.png")
    assert shot_score.main([str(tmp_path), "--fail-under", "1"]) == 0


# --- Framed store shots -----------------------------------------------------
#
# The watch-body check has two detectors of very different quality, and the
# severity of a miss must follow which one ran: a Hough miss is a real
# failure, a miss by the brittle no-OpenCV fallback is only a warning.

def _framed(bg=tuple(int(c) for c in shot_score.STORE_BG)):
    img = Image.new("RGB", (1080, 1080), bg)
    img.paste(_watch_face(454), (313, 313))
    return img


def _fake_cv2(circles):
    import types
    import numpy as np
    return types.SimpleNamespace(
        HOUGH_GRADIENT=3,
        HoughCircles=lambda *a, **k: None if circles is None
        else np.array([circles], dtype=np.float32),
    )


def _assess_framed(tmp_path, monkeypatch, *, cv2, bg=None):
    if cv2 is None:
        monkeypatch.setattr(shot_score, "HAS_CV2", False)
    else:
        monkeypatch.setattr(shot_score, "HAS_CV2", True)
        monkeypatch.setattr(shot_score, "cv2", cv2, raising=False)
        # The stub only speaks HoughCircles; sharpness is not under test.
        monkeypatch.setattr(shot_score, "_laplacian_variance", lambda g: 500.0)
    p = tmp_path / "framed.png"
    (_framed() if bg is None else _framed(bg)).save(p)
    r = shot_score.assess(p)
    assert r.matched_kind == "framed"
    return r


def test_a_fallback_miss_on_a_framed_shot_only_warns(tmp_path, monkeypatch):
    r = _assess_framed(tmp_path, monkeypatch, cv2=None)
    body = {f.code: f.severity for f in r.findings if "watch_body" in f.code}
    assert body == {"watch_body_unverified": shot_score.SEVERITY_WARN}


def test_a_hough_miss_on_a_framed_shot_fails(tmp_path, monkeypatch):
    r = _assess_framed(tmp_path, monkeypatch, cv2=_fake_cv2(None))
    body = {f.code: f.severity for f in r.findings if "watch_body" in f.code}
    assert body == {"no_watch_body_detected": shot_score.SEVERITY_FAIL}


def test_a_centred_watch_body_is_reported_not_flagged(tmp_path, monkeypatch):
    r = _assess_framed(tmp_path, monkeypatch, cv2=_fake_cv2([[540, 540, 227]]))
    assert r.metrics["watch_circle"]["centeredness"] == 1.0
    assert not {"watch_off_centre", "no_watch_body_detected"} & _codes(r)


def test_the_most_centred_circle_wins_and_off_centre_warns(tmp_path, monkeypatch):
    # Hough's first candidate is not its best one: selection is by centredness.
    r = _assess_framed(tmp_path, monkeypatch,
                       cv2=_fake_cv2([[900, 900, 200], [700, 700, 227]]))
    assert r.metrics["watch_circle"]["cx"] == 700.0
    assert "watch_off_centre" in _codes(r)


def test_a_framed_shot_off_the_store_background_warns(tmp_path, monkeypatch):
    on_bg = _assess_framed(tmp_path, monkeypatch, cv2=None)
    assert "framed_bg_off" not in _codes(on_bg)
    off_bg = _assess_framed(tmp_path, monkeypatch, cv2=None, bg=(200, 200, 200))
    assert "framed_bg_off" in _codes(off_bg)


def test_the_fallback_detector_finds_a_dark_centre_on_light_corners(monkeypatch):
    import numpy as np
    monkeypatch.setattr(shot_score, "HAS_CV2", False)
    gray = np.full((400, 400), 200.0)
    yy, xx = np.ogrid[:400, :400]
    gray[(xx - 200) ** 2 + (yy - 200) ** 2 <= 150 ** 2] = 20.0
    assert shot_score._detect_central_dark_circle(gray)["centeredness"] == 1.0
    assert shot_score._detect_central_dark_circle(np.full((400, 400), 90.0)) is None


# --- Icons and covers -------------------------------------------------------

def test_a_solid_icon_fails(tmp_path):
    p = tmp_path / "icon.png"
    Image.new("RGB", (128, 128), (40, 90, 200)).save(p)
    r = shot_score.assess(p)
    assert r.matched_kind == "icon"
    assert "icon_blank_or_solid" in _codes(r)


def test_a_flat_cover_warns_and_a_decorated_one_does_not(tmp_path):
    flat = tmp_path / "flat.png"
    Image.new("RGB", (500, 500), (30, 30, 30)).save(flat)
    assert "cover_too_flat" in _codes(shot_score.assess(flat))

    busy = Image.new("RGB", (500, 500))
    busy.putdata([((x * 7) % 256, (y * 5) % 256, ((x + y) * 3) % 256)
                  for y in range(500) for x in range(500)])
    p = tmp_path / "busy.png"
    busy.save(p)
    r = shot_score.assess(p)
    assert r.matched_kind == "cover"
    assert "cover_too_flat" not in _codes(r)
