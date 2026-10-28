"""SDK discovery, the environment preflight, and OCR backend selection.

Preflight exists to turn a confusing symptom into one actionable message, so
what it *says* is the feature. OCR's contract is subtler: a missing backend
must degrade every check to "skipped", never to "failed", or every machine
without one starts failing captures that are perfectly fine.
"""
import os
import sys

import pytest

from ciqkit.sim import ocr, preflight


@pytest.fixture(autouse=True)
def _fresh():
    preflight.reset()
    yield
    preflight.reset()


# --- SDK discovery -----------------------------------------------------------

def test_ciq_home_overrides_the_search(tmp_path, monkeypatch):
    monkeypatch.setenv("CIQ_HOME", str(tmp_path))
    assert preflight.detect_sdk() == str(tmp_path)


def test_ciq_home_is_user_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))   # what Windows expands ~ to
    monkeypatch.setenv("CIQ_HOME", "~/sdk")
    assert preflight.detect_sdk() == str(tmp_path / "sdk")


def test_the_newest_installed_sdk_wins(tmp_path, monkeypatch):
    monkeypatch.delenv("CIQ_HOME", raising=False)
    for name in ("connectiq-sdk-mac-7.1.0", "connectiq-sdk-mac-9.1.0",
                 "connectiq-sdk-mac-8.0.0"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(preflight, "sdk_root", lambda: str(tmp_path))
    assert preflight.detect_sdk().endswith("9.1.0")


def test_a_two_digit_minor_version_still_sorts_as_newest(tmp_path, monkeypatch):
    """Alphabetically 9.9.0 beats 9.10.0, so "newest" needs a numeric compare.

    This bites exactly once, silently, on the day Garmin ships a two-digit
    minor: every capture starts building against an older SDK than the one the
    user installed.
    """
    monkeypatch.delenv("CIQ_HOME", raising=False)
    for name in ("connectiq-sdk-mac-9.9.0-2025-11-04-aaaaaa",
                 "connectiq-sdk-mac-9.10.0-2026-02-11-bbbbbb",
                 "connectiq-sdk-mac-10.0.0-2026-06-01-cccccc"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(preflight, "sdk_root", lambda: str(tmp_path))
    assert "10.0.0" in preflight.detect_sdk()


def test_the_newest_sdk_of_the_same_version_line_wins(tmp_path, monkeypatch):
    monkeypatch.delenv("CIQ_HOME", raising=False)
    for name in ("connectiq-sdk-mac-9.10.0-2026-02-11-aaaaaa",
                 "connectiq-sdk-mac-9.9.0-2026-03-01-bbbbbb"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(preflight, "sdk_root", lambda: str(tmp_path))
    assert "9.10.0" in preflight.detect_sdk()


def test_no_sdk_installed_yields_no_path(tmp_path, monkeypatch):
    monkeypatch.delenv("CIQ_HOME", raising=False)
    monkeypatch.setattr(preflight, "sdk_root", lambda: str(tmp_path))
    assert preflight.detect_sdk() == ""


def test_an_unknown_platform_has_no_sdk_root(monkeypatch):
    monkeypatch.setattr(sys, "platform", "sunos5")
    assert preflight.sdk_root() == ""


def test_the_build_reads_devices_from_the_same_place_as_preflight(tmp_path, monkeypatch):
    # build.py once kept its own copy of the macOS path, so on Windows doctor
    # and `ciqkit new` looked under ~/Library and reported no devices at all.
    from ciqkit import build
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    expected = os.path.join(str(tmp_path), "Garmin", "ConnectIQ", "Devices")
    assert preflight.devices_root() == expected
    assert build.sdk_devices_dir() == expected


# --- what preflight tells the user ------------------------------------------

def test_a_missing_sdk_points_at_the_sdk_manager(monkeypatch):
    # The SDK message is the subject, on every CI platform: on Linux the
    # platform check would otherwise refuse first.
    monkeypatch.setattr(preflight, "_check_platform", lambda w: None)
    monkeypatch.setattr(preflight, "detect_sdk", lambda: "")
    with pytest.raises(preflight.PreflightError) as exc:
        preflight.check_once()
    msg = str(exc.value)
    assert "SDK Manager" in msg and "CIQ_HOME" in msg


def test_a_directory_that_is_not_an_sdk_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "_check_platform", lambda w: None)
    monkeypatch.setattr(preflight, "detect_sdk", lambda: str(tmp_path))
    with pytest.raises(preflight.PreflightError, match="does not look like"):
        preflight.check_once()


def test_an_unsupported_platform_says_so_rather_than_failing_obscurely(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(preflight.PreflightError) as exc:
        preflight.check_once()
    assert "macOS and Windows" in str(exc.value)


def test_the_result_is_memoized(tmp_path, monkeypatch):
    """A long-running daemon must not re-probe on every job."""
    calls = []

    def fake_detect():
        calls.append(1)
        return str(tmp_path)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "ConnectIQ.app").mkdir()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(preflight, "detect_sdk", fake_detect)
    monkeypatch.setattr(preflight, "_check_accessibility", lambda w: None)
    preflight.check_once()
    preflight.check_once()
    assert len(calls) == 1


def test_a_failure_is_memoized_too(monkeypatch):
    monkeypatch.setattr(preflight, "detect_sdk", lambda: "")
    with pytest.raises(preflight.PreflightError):
        preflight.check_once()
    with pytest.raises(preflight.PreflightError):
        preflight.check_once()


def test_running_over_ssh_is_a_warning_not_a_failure(tmp_path, monkeypatch):
    """The simulator may still work on a local console session; only the user
    can judge, so this must not block the run."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "ConnectIQ.app").mkdir()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(preflight, "detect_sdk", lambda: str(tmp_path))
    monkeypatch.setattr(preflight, "_check_accessibility", lambda w: None)
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 1 5.6.7.8 22")
    res = preflight.check_once()
    assert res["ok"] is True
    assert any("SSH" in w for w in res["warnings"])


# --- OCR: a missing backend must skip, never fail ---------------------------

def test_no_backend_means_anchors_are_skipped(monkeypatch):
    monkeypatch.setattr(ocr, "backend", lambda: None)
    assert ocr.contains_any(object(), ["anything"]) is None


def test_no_anchors_means_nothing_to_check():
    assert ocr.contains_any(object(), []) is None
    assert ocr.contains_any(object(), None) is None


def test_empty_recognised_text_is_skipped_not_failed(monkeypatch):
    """OCR that returns nothing is an unreliable reading, not proof of absence."""
    monkeypatch.setattr(ocr, "backend", lambda: "vision")
    monkeypatch.setattr(ocr, "text", lambda _img: "")
    assert ocr.contains_any(object(), ["total"]) is None


def test_a_present_anchor_matches_case_insensitively(monkeypatch):
    monkeypatch.setattr(ocr, "backend", lambda: "vision")
    monkeypatch.setattr(ocr, "text", lambda _img: "opponent g1 0-0 me")
    assert ocr.contains_any(object(), ["OPPONENT"]) is True


def test_any_one_anchor_is_enough(monkeypatch):
    monkeypatch.setattr(ocr, "backend", lambda: "vision")
    monkeypatch.setattr(ocr, "text", lambda _img: "total 12")
    assert ocr.contains_any(object(), ["missing", "TOTAL"]) is True


def test_an_absent_anchor_is_a_definite_no(monkeypatch):
    monkeypatch.setattr(ocr, "backend", lambda: "vision")
    monkeypatch.setattr(ocr, "text", lambda _img: "total 12")
    assert ocr.contains_any(object(), ["settings"]) is False


def test_text_returns_empty_when_no_backend_is_available(monkeypatch):
    monkeypatch.setattr(ocr, "backend", lambda: None)
    assert ocr.text(object()) == ""


def test_a_backend_that_raises_degrades_to_empty_text(monkeypatch):
    """A broken OCR install must not take a capture down with it."""
    monkeypatch.setattr(ocr, "backend", lambda: "vision")
    monkeypatch.setattr(ocr, "_BACKEND", "vision", raising=False)

    class Boom:
        def __getattr__(self, _n):
            raise RuntimeError("boom")
    assert ocr.text(Boom()) == ""



def _only_backend(monkeypatch, name, module):
    """Make `name` the one importable OCR backend, whatever this machine has.

    A None entry in sys.modules makes `import` raise ImportError, which is how
    a missing backend looks to `backend()`.
    """
    for mod in ("ocrmac", "ocrmac.ocrmac", "winocr", "pytesseract"):
        monkeypatch.setitem(sys.modules, mod, None)
    monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(ocr, "_checked", False)
    monkeypatch.setattr(ocr, "_BACKEND", None)


def test_backend_selection_prefers_windows_ocr_over_tesseract(monkeypatch):
    import types
    _only_backend(monkeypatch, "pytesseract", types.ModuleType("pytesseract"))
    monkeypatch.setitem(sys.modules, "winocr", types.ModuleType("winocr"))
    assert ocr.backend() == "windows"


def test_backend_selection_is_none_without_any_backend(monkeypatch):
    import types
    _only_backend(monkeypatch, "winocr", types.ModuleType("winocr"))
    monkeypatch.setitem(sys.modules, "winocr", None)
    assert ocr.backend() is None
    assert ocr.contains_any(object(), ["total"]) is None


def test_windows_ocr_uses_the_configured_language(monkeypatch):
    """A localised Windows may lack the English OCR pack; CIQKIT_OCR_LANG
    picks the one it has."""
    import types

    from PIL import Image
    seen = {}

    def recognize_pil_sync(img, lang):
        seen["mode"], seen["lang"] = img.mode, lang
        return {"text": "Total 12"}
    winocr = types.ModuleType("winocr")
    winocr.recognize_pil_sync = recognize_pil_sync
    _only_backend(monkeypatch, "winocr", winocr)
    monkeypatch.setenv("CIQKIT_OCR_LANG", "de")

    assert ocr.text(Image.new("L", (4, 4))) == "total 12"
    assert seen == {"mode": "RGB", "lang": "de"}


def test_windows_ocr_missing_language_pack_skips_the_anchor(monkeypatch):
    """A missing pack raises inside winocr: that is "no text", so the anchor
    is skipped -- never reported as contradicted."""
    import types

    from PIL import Image

    def recognize_pil_sync(_img, _lang):
        raise OSError("language pack not installed")
    winocr = types.ModuleType("winocr")
    winocr.recognize_pil_sync = recognize_pil_sync
    _only_backend(monkeypatch, "winocr", winocr)

    assert ocr.contains_any(Image.new("RGB", (4, 4)), ["total"]) is None


def test_tesseract_reads_an_upscaled_greyscale_copy(monkeypatch):
    import types

    from PIL import Image
    seen = {}

    def image_to_string(img):
        seen["mode"], seen["size"] = img.mode, img.size
        return "SETTINGS\n"
    tess = types.ModuleType("pytesseract")
    tess.image_to_string = image_to_string
    _only_backend(monkeypatch, "pytesseract", tess)

    assert ocr.contains_any(Image.new("RGB", (10, 6)), ["settings"]) is True
    assert seen == {"mode": "L", "size": (30, 18)}

# --- preflight memoization ---------------------------------------------------

def test_a_preflight_failure_is_not_cached(monkeypatch):
    """Every condition preflight checks is one the user fixes WHILE the daemon
    runs -- granting Accessibility, installing an SDK, moving off VNC. Caching
    the failure meant the fix appeared not to take, because a background broker
    was still holding a three-minute-old "no" for up to ten idle minutes.
    """
    from ciqkit.sim import preflight

    preflight.reset()
    calls = []

    def flaky(warnings):
        calls.append(1)
        if len(calls) == 1:
            raise preflight.PreflightError("grant Accessibility")

    monkeypatch.setattr(preflight, "_check_platform", lambda w: None)
    monkeypatch.setattr(preflight, "_check_sdk", lambda w: "/fake/sdk")
    monkeypatch.setattr(preflight, "_check_accessibility", flaky)
    monkeypatch.setattr(preflight, "_check_remote", lambda w: None)

    with pytest.raises(preflight.PreflightError):
        preflight.check_once()
    # The user grants the permission; the very next job must see it.
    assert preflight.check_once()["ok"] is True
    assert len(calls) == 2, "the failure was cached instead of retried"
    preflight.reset()


def test_a_preflight_pass_is_cached(monkeypatch):
    """A long-running daemon must not re-run subprocesses on every job."""
    from ciqkit.sim import preflight

    preflight.reset()
    calls = []
    monkeypatch.setattr(preflight, "_check_platform", lambda w: calls.append(1))
    monkeypatch.setattr(preflight, "_check_sdk", lambda w: "/fake/sdk")
    monkeypatch.setattr(preflight, "_check_accessibility", lambda w: None)
    monkeypatch.setattr(preflight, "_check_remote", lambda w: None)

    preflight.check_once()
    preflight.check_once()
    assert len(calls) == 1
    preflight.reset()
