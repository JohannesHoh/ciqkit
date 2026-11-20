"""Frame verification predicates: pure functions of images and plain parameters.

Split out of ``SimSession`` so the "is this a real app frame / did the screen
change" proofs can be tested headless, with synthetic images. Whatever session
state they used to read (device resolution, the declared display circle, the
face cropper, the other predicates) is an explicit argument; ``SimSession``
keeps thin same-named methods that pass it in, so patching a method on the
session still redirects every caller. No GUI and no ``sim`` imports.
"""
from __future__ import annotations

import logging

from . import geometry as su

log = logging.getLogger("ciqkit.sim")


def is_app_frame(img, before_sig, saw_loader: bool = False, *,
                 is_blank, is_loading_screen, face_sig,
                 signatures_differ=su.signatures_differ) -> bool:
    """``is_blank`` / ``is_loading_screen`` / ``face_sig`` are callables on one image.

    ``signatures_differ`` is injectable so the session can hand in its (patchable)
    ``su.signatures_differ``."""
    if is_blank(img):
        return False
    if is_loading_screen(img):
        # Connect IQ's own pre-load screen. It satisfies both the
        # not-blank and the changed-from-the-watchface tests, so without
        # this the load "succeeds" and the screenshot is of the loader --
        # exactly the confidently-wrong output this class is meant to
        # prevent. Keep polling instead.
        return False
    if saw_loader:
        # We watched the loading screen come and go since the push, so this
        # frame is the app that just finished loading. Accept it without a
        # frame diff: re-running the same build shows a screen IDENTICAL to
        # the one already up, and requiring a difference would time out and
        # trigger a pointless simulator restart every time.
        return True
    if before_sig is not None:
        # App loaded => the round face changed from the pre-push frame.
        return signatures_differ(before_sig, face_sig(img))
    # No baseline. This used to `return True` -- "any non-blank frame
    # counts" -- on the theory that it only happens on a cold start. It
    # does not: `_safe_sig` swallows every capture error, so a single
    # transient grab failure at push time landed here too, and then a
    # silently failed push left the DEVICE WATCHFACE on screen. Not blank,
    # not the loader, no baseline to differ from -> "verified". The one
    # thing this class exists to prevent.
    #
    # So: no baseline means the frame-diff leg of the proof is missing, and
    # this frame cannot carry the load on its own. Keep polling; if the
    # loader shows up, or an anchor matches, those are real evidence. If
    # neither ever does, the load fails and says why.
    log.debug("no pre-push baseline: refusing to accept a frame on "
              "not-blank alone")
    return False


def is_loading_screen(img, crop_face) -> bool:
    """Is this Connect IQ's pre-load screen (a blue triangle on black)?

    Keyed on a signature that real app screens do not have: an almost
    entirely black face carrying a small strongly blue-dominant mark and
    essentially nothing else. A real dark screen has text or chrome, which
    lands in "other" and pushes it past the threshold, so this does not
    false-reject a legitimately minimal screen.

    ``crop_face(img)`` returns the face crop or None (then the whole image).
    """
    try:
        import numpy as np
    except ImportError:
        return False
    try:
        face = crop_face(img) or img
        a = np.asarray(face.convert("RGB"), dtype=np.int16)
        if a.size == 0:
            return False
        r, g, b = a[..., 0], a[..., 1], a[..., 2]
        luma = 0.299 * r + 0.587 * g + 0.114 * b

        dark = luma < 24
        blue = (b > r + 40) & (b > g + 40) & (b > 60)
        other = ~dark & ~blue

        # Only the round display counts. The crop is its bounding SQUARE,
        # and where the simulator draws the device (always, on Windows)
        # the square's corners are grey bezel and tick marks -- ~21 % of
        # the area, all "other", which hid the loader from this check.
        h, w = dark.shape
        yy, xx = np.ogrid[:h, :w]
        inside = ((xx - (w - 1) / 2.0) ** 2 / max(1.0, (w / 2.0) ** 2)
                  + (yy - (h - 1) / 2.0) ** 2 / max(1.0, (h / 2.0) ** 2)) <= 0.95
        total = float(inside.sum()) or 1.0
        dark_f = (dark & inside).sum() / total
        blue_f = (blue & inside).sum() / total
        other_f = (other & inside).sum() / total
        return bool(dark_f >= 0.85 and 0.003 <= blue_f <= 0.12
                    and other_f <= 0.03)
    except Exception:
        return False


def is_blank(img) -> bool:
    """Near-uniform frame = blank / launcher / solid watchface load screen."""
    try:
        from PIL import ImageStat
        st = ImageStat.Stat(img.convert("L"))
        return (st.stddev[0] if st.stddev else 0.0) < 6.0
    except Exception:
        return False


def face_circle(img, device_res, display_circle):
    """(cx, cy, radius) of the watch face.

    ``device_res`` snaps the detected radius; ``display_circle`` is the circle
    from the device profile's declared display rect, or None.
    """
    # find_watch_face_auto returns (cx, cy, radius) -- exactly what the
    # signature functions want. Pass the device resolution so it snaps the
    # radius correctly; fall back to a centred guess.
    # The declared display rect first: it does not move with the screen's
    # content, so before/after signatures compare the same pixels.
    r = display_circle
    if r is None:
        try:
            r = su.find_watch_face_auto(img, device_res=device_res)
        except Exception:
            r = None
    if r and len(r) == 3:
        return r
    w, h = img.size
    return w // 2, h // 2, min(w, h) // 4


def face_sig(img, circle):
    """Face-region signature of ``img`` over ``circle`` = (cx, cy, radius)."""
    return su.face_region_signature(img, *circle)


def frame_detail(img):
    return su.frame_detail_signature(img)


def input_changed(before, after, face_sig, frame_detail, *,
                  signatures_differ=su.signatures_differ,
                  details_differ=su.details_differ) -> bool:
    """Did a gesture change the screen? The coarse whole-face test, OR a
    strong local change (see ``details_differ``): placing one mark on a dark
    game board moves the 4x4 average by a few levels, far under the coarse
    threshold, and the retry that followed was a second, real move.

    ``face_sig`` / ``frame_detail`` are callables on one image; the two
    differ functions are injectable like in ``is_app_frame``.
    """
    if signatures_differ(face_sig(before), face_sig(after)):
        return True
    return details_differ(frame_detail(before), frame_detail(after))
