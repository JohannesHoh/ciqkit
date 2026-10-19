"""OCR text extraction for screen-content verification.

Self-contained (no other ciqkit imports). Backend order: Apple Vision via
`ocrmac` (best on macOS), the Windows OCR engine via `winocr` (built into
Windows 10/11; ~0.14 s a frame), then `pytesseract` (3x upscale + greyscale),
else OCR is disabled and every check degrades to `None` ("skip", never
"fail").
"""
from __future__ import annotations

import os

_BACKEND = None          # "vision" | "windows" | "tesseract" | None
_checked = False


def backend() -> str | None:
    global _BACKEND, _checked
    if _checked:
        return _BACKEND
    _checked = True
    try:
        import ocrmac.ocrmac  # noqa: F401
        _BACKEND = "vision"
        return _BACKEND
    except Exception:
        pass
    try:
        import winocr  # noqa: F401
        _BACKEND = "windows"
        return _BACKEND
    except Exception:
        pass
    try:
        import pytesseract  # noqa: F401
        _BACKEND = "tesseract"
        return _BACKEND
    except Exception:
        pass
    _BACKEND = None
    return _BACKEND


def text(img) -> str:
    """Return lower-cased recognized text from a PIL image ("" if no backend)."""
    b = backend()
    if b == "vision":
        try:
            from ocrmac.ocrmac import OCR
            res = OCR(img).recognize()
            return " ".join(t for (t, _conf, _box) in res).lower()
        except Exception:
            return ""
    if b == "windows":
        try:
            import winocr
            # The recogniser needs the language's OCR pack, which a localised
            # Windows may not have for English; CIQKIT_OCR_LANG picks another.
            # A missing pack raises, and that is "no text" -- a skip.
            lang = os.environ.get("CIQKIT_OCR_LANG", "en")
            res = winocr.recognize_pil_sync(img.convert("RGB"), lang)
            return (res.get("text") or "").lower()
        except Exception:
            return ""
    if b == "tesseract":
        try:
            import pytesseract
            g = img.convert("L").resize((img.width * 3, img.height * 3))
            return pytesseract.image_to_string(g).lower()
        except Exception:
            return ""
    return ""


def contains_any(img, anchors) -> bool | None:
    """True/False if any anchor substring is present; None when OCR is disabled
    (caller treats None as 'skip', not 'fail')."""
    if not anchors:
        return None
    if backend() is None:
        return None
    t = text(img)
    if not t:
        return None
    return any(a.lower() in t for a in anchors)
