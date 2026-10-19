"""record() framing: detect once, frame every frame, zero drift.

The bug this guards: record() used to crop every frame to a tight face box
while shot() framed the complete watch, so recorded GIFs clipped the top of
the scene and sliced the watch body mid-bezel. The fix frames each frame
exactly like shot() -- but with the geometry detected ONCE on the first frame,
because the detection reads the frame's content and re-running it per frame
makes the clip wobble (test_capture_geometry proves the detector moves under
bright content).

Runs without a simulator: the window grab is stubbed with synthetic
device-art frames.
"""
import glob
import os

import pytest

np = pytest.importorskip("numpy")
PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw  # noqa: E402

from ciqkit.sim import session as session_mod  # noqa: E402
from ciqkit.sim.session import SimSession  # noqa: E402


def device_art_frame(content=None):
    W, H = 700, 900
    img = Image.new("RGB", (W, H), (250, 250, 250))
    d = ImageDraw.Draw(img)
    d.rectangle([300, 120, 400, 780], fill=(30, 30, 30))    # strap
    d.ellipse([170, 270, 530, 630], fill=(20, 20, 20))      # body
    d.ellipse([200, 300, 500, 600], fill=(0, 0, 0))         # screen
    if content:
        content(d)
    return img


def _bright(d):
    d.rectangle([230, 330, 470, 470], fill=(255, 255, 255))


@pytest.fixture
def rec_session(tmp_path, monkeypatch):
    """A SimSession whose GUI plumbing is stubbed out."""
    s = SimSession.__new__(SimSession)
    s._seed_backup = None
    s._warned_devices = set()
    s.current = None
    s.out_dir = str(tmp_path)
    s._hwnd = object()
    s._rebind = lambda: None
    monkeypatch.setattr(session_mod.su, "dismiss_modal_dialogs",
                        lambda **_k: None)
    monkeypatch.setattr(session_mod.su, "restore_window", lambda _h: None)
    return s


def _run_record(s, frames, **kw):
    it = iter(frames)
    last = frames[-1]
    s.capture_calls = []

    def _cap(**cap_kw):
        s.capture_calls.append(cap_kw)
        return next(it, last)

    s._capture = _cap
    kw.setdefault("settle_s", 0)
    kw.setdefault("interval_ms", 1)
    kw.setdefault("duration_s", 60)
    kw.setdefault("max_frames", len(frames))
    kw.setdefault("keep_frames", True)
    return s.record("clip", **kw)


def _dark_bbox(png_path):
    a = np.asarray(Image.open(png_path).convert("RGB")).min(axis=2)
    ys, xs = np.where(a < 100)
    return (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))


def test_recorded_frames_are_framed_like_shot_and_do_not_drift(
        rec_session, monkeypatch):
    """Every frame is the COMPLETE framed watch (store-size square), and the
    watch sits on identical pixels in the first and last frame even though the
    screen content changed between them."""
    # Three DISTINCT contents: PIL's GIF writer merges a frame identical to
    # its predecessor, which is fine for real clips but would hide a frame
    # here.
    def _dot(d):
        d.ellipse([340, 440, 360, 460], fill=(255, 0, 0))

    res = _run_record(rec_session, [device_art_frame(),
                                    device_art_frame(_dot),
                                    device_art_frame(_bright)])
    assert res["framed"] is True
    assert res["frames"] == 3
    pngs = sorted(glob.glob(os.path.join(res["frames_dir"], "*.png")))
    assert len(pngs) == 3
    from ciqkit.capture.framing import STORE_SCREENSHOT_SIZE
    for p in pngs:
        assert Image.open(p).size == (STORE_SCREENSHOT_SIZE,) * 2
    # No drift: the watch's dark-pixel bounding box is pixel-identical between
    # the first frame and the (content-changed) last frame.
    assert _dark_bbox(pngs[0]) == _dark_bbox(pngs[-1])
    assert os.path.exists(res["gif"])
    with Image.open(res["gif"]) as gif:
        assert gif.n_frames == 3
        assert gif.size == (STORE_SCREENSHOT_SIZE,) * 2


def test_the_framing_geometry_is_detected_exactly_once(rec_session, monkeypatch):
    """Per-frame re-detection is the wobble bug; the clip must reuse frame 0's
    geometry."""
    calls = {"n": 0}
    real = session_mod.su.detect_device_art_geometry

    def counting(img):
        calls["n"] += 1
        return real(img)

    monkeypatch.setattr(session_mod.su, "detect_device_art_geometry", counting)
    _run_record(rec_session, [device_art_frame(), device_art_frame(_bright),
                              device_art_frame()])
    assert calls["n"] == 1


def test_native_mode_keeps_the_old_tight_crop(rec_session):
    """frame=False preserves the pre-framing behaviour for callers that want
    the bare face."""
    res = _run_record(rec_session, [device_art_frame(), device_art_frame()],
                      frame=False)
    assert res["framed"] is False
    pngs = sorted(glob.glob(os.path.join(res["frames_dir"], "*.png")))
    sizes = {Image.open(p).size for p in pngs}
    assert len(sizes) == 1                      # still one stable crop
    from ciqkit.capture.framing import STORE_SCREENSHOT_SIZE
    assert (STORE_SCREENSHOT_SIZE,) * 2 not in sizes


def test_an_undetectable_first_frame_falls_back_instead_of_failing(rec_session):
    """A frame with no watch in it (blank grey window) still records -- as raw
    frames -- and says so via framed=False."""
    blank = Image.new("RGB", (700, 900), (128, 128, 128))
    res = _run_record(rec_session, [blank, blank])
    assert res["framed"] is False
    assert res["frames"] == 2


# ── capture rate: the raise/settle must not run per frame ────────────────────
# record() used to sample at ~0.5 fps on macOS, and the blame landed on the
# Garmin simulator. It was ours: capture_window() re-probed the window position
# (0.13 s), restored it (0.15 s), focused it (0.57 s) and slept 1.0 s BEFORE
# every grab -- ~1.85 s of every ~2.1 s frame was window bookkeeping the loop
# had already done once up front. These guard the fix from being quietly undone.
# Asserted key by key, not as a whole dict: a future kwarg on the _capture call
# should not break every test here.

def test_only_the_first_frame_pays_the_raise_and_settle(rec_session):
    _run_record(rec_session, [device_art_frame() for _ in range(6)],
                refocus_every=0)
    calls = rec_session.capture_calls
    assert len(calls) == 6
    # Frame 0: full raise, default (None -> 1 s) settle -- the whole clip's
    # framing geometry is read off it.
    assert calls[0]["refocus"] is True
    assert calls[0]["grab_settle_s"] is None
    # Every other frame: no AppleScript, no settle.
    assert all(c["refocus"] is False for c in calls[1:])
    assert all(c["grab_settle_s"] == 0.0 for c in calls[1:])


def test_the_window_is_re_raised_periodically_as_an_occlusion_guard(rec_session):
    _run_record(rec_session, [device_art_frame() for _ in range(7)],
                refocus_every=3)
    calls = rec_session.capture_calls
    assert [i for i, c in enumerate(calls) if c["refocus"]] == [0, 3, 6]
    # ...but never at the cost of the 1 s settle again: only frame 0 gets that.
    assert all(c["grab_settle_s"] != 0.0 or not c["refocus"] for c in calls[1:])
    assert max(c["grab_settle_s"] for c in calls[1:]) <= 0.2


def test_a_re_raised_frame_still_settles_a_little(rec_session):
    """focus_window returns 0.3 s after the AppleScript. A window that really
    was buried may not be forward yet, so the one frame whose job is to escape
    the occluder must not grab with zero settle -- it would capture it."""
    _run_record(rec_session, [device_art_frame() for _ in range(7)],
                refocus_every=3)
    calls = rec_session.capture_calls
    assert calls[3]["grab_settle_s"] > 0 and calls[6]["grab_settle_s"] > 0
    assert calls[1]["grab_settle_s"] == 0.0   # ...but a plain frame still does


def test_the_default_refocus_cadence_stays_rare():
    """A raise costs ~0.85 s against a ~0.22 s frame, so at the default cadence
    it must stay a few percent of a clip, not a tax on it."""
    assert SimSession.DEFAULT_REFOCUS_EVERY >= 10


# ── the canvas flood is the other half of the old ~0.5 fps ───────────────────
# Framing a frame costs ~0.48 s, and ~0.47 s of that is one pure-Python
# scanline flood fill that repaints the simulator's white canvas. The region it
# finds is OUTSIDE the bezel, so it is identical on every frame of a clip:
# detect it once as a mask, then paste.

def test_the_canvas_mask_reproduces_the_flood_fill_exactly(rec_session):
    """The fast path is not an approximation -- masked output must be
    pixel-identical to flooding every frame, or the speedup is a regression."""
    from ciqkit.capture import framing
    img = device_art_frame(_bright)
    box = framing.detect_device_art_geometry(img)
    assert box is not None
    slow = framing.apply_device_art_frame(img, box)
    mask = framing.device_art_canvas_mask(img, box)
    fast = framing.apply_device_art_frame(img, box, canvas_mask=mask)
    from PIL import ImageChops
    assert np.asarray(ImageChops.difference(slow, fast)).max() == 0


def test_in_display_whites_survive_the_mask(rec_session):
    """The flood deliberately spares white INSIDE the screen (it never touches
    the crop border). A mask that swallowed it would black out real content."""
    from ciqkit.capture import framing
    img = device_art_frame(_bright)
    box = framing.detect_device_art_geometry(img)
    framed = framing.apply_device_art_frame(
        img, box, canvas_mask=framing.device_art_canvas_mask(img, box))
    a = np.asarray(framed.convert("RGB"))
    assert (a.min(axis=2) > 200).sum() > 500      # the bright patch is still there


def test_a_wrong_sized_mask_is_ignored_rather_than_trusted(rec_session):
    """A mask sized for a different crop would repaint the wrong pixels. Wrong
    size => fall back to the (slow, correct) flood."""
    from ciqkit.capture import framing
    img = device_art_frame(_bright)
    box = framing.detect_device_art_geometry(img)
    bogus = Image.new("L", (7, 7), 255)
    out = framing.apply_device_art_frame(img, box, canvas_mask=bogus)
    from PIL import ImageChops
    assert np.asarray(ImageChops.difference(
        out, framing.apply_device_art_frame(img, box))).max() == 0


def test_a_mask_whose_canvas_went_dark_is_rejected(rec_session):
    """The size check alone is not a staleness check: PIL pads an out-of-bounds
    crop, so a crop taken after the window MOVED is still exactly box-sized
    while describing different pixels. Applying the old mask to it would
    repaint the watch. Content is what has to be checked."""
    from ciqkit.capture import framing
    img = device_art_frame()
    box = framing.detect_device_art_geometry(img)
    mask = framing.device_art_canvas_mask(img, box)
    assert framing._canvas_mask_still_fits(img.convert("RGB").crop(box), mask)
    # Same geometry, but the window is gone: the region is now dark desktop.
    moved = Image.new("RGB", img.size, (20, 20, 20))
    assert not framing._canvas_mask_still_fits(
        moved.convert("RGB").crop(box), mask)
    # ...and apply_device_art_frame must decline the mask, not paste it.
    from PIL import ImageChops
    assert np.asarray(ImageChops.difference(
        framing.apply_device_art_frame(moved, box, canvas_mask=mask),
        framing.apply_device_art_frame(moved, box))).max() == 0


def test_a_slid_window_does_not_pass_the_fit_check(rec_session):
    """The realistic move: the watch is still on screen, just shifted, so the
    crop is full of real content and only misaligned."""
    from ciqkit.capture import framing
    img = device_art_frame()
    box = framing.detect_device_art_geometry(img)
    mask = framing.device_art_canvas_mask(img, box)
    slid = Image.new("RGB", img.size, (250, 250, 250))
    slid.paste(img, (-160, -110))          # window moved up and left
    assert not framing._canvas_mask_still_fits(
        slid.convert("RGB").crop(box), mask)


def test_a_clip_builds_the_canvas_mask_once(rec_session, monkeypatch):
    """Per-frame mask construction would put the flood fill straight back."""
    calls = {"n": 0}
    real = session_mod.su.device_art_canvas_mask

    def counting(img, box, *a, **kw):
        calls["n"] += 1
        return real(img, box, *a, **kw)

    monkeypatch.setattr(session_mod.su, "device_art_canvas_mask", counting)
    res = _run_record(rec_session, [device_art_frame(), device_art_frame(_bright),
                                    device_art_frame()])
    assert res["framed"] is True
    assert calls["n"] == 1


def test_a_failed_mask_still_records(rec_session, monkeypatch):
    """The mask is an optimisation. If it raises, the clip must fall back to
    flooding every frame, not fail."""
    def boom(*_a, **_kw):
        raise RuntimeError("no mask for you")

    monkeypatch.setattr(session_mod.su, "device_art_canvas_mask", boom)
    res = _run_record(rec_session, [device_art_frame(), device_art_frame(_bright)])
    assert res["ok"] is True and res["framed"] is True and res["frames"] == 2


def test_the_canvas_mask_is_rebuilt_on_a_re_raised_frame(rec_session, monkeypatch):
    """Building the mask ONCE bakes frame 0's accidents into the whole clip.
    The realistic one: something dark (a notification banner) sits over the
    white canvas at frame 0, blocks the flood there, and every later frame
    keeps an un-repainted white patch -- a regression against the old
    per-frame flood, which self-healed. Rebuilding on each re-raised frame
    bounds that to one refocus interval, on a frame that is already paying for
    the raise."""
    calls = {"n": 0}
    real = session_mod.su.device_art_canvas_mask

    def counting(img, box, *a, **kw):
        calls["n"] += 1
        return real(img, box, *a, **kw)

    monkeypatch.setattr(session_mod.su, "device_art_canvas_mask", counting)
    _run_record(rec_session, [device_art_frame() for _ in range(7)],
                refocus_every=3)
    # frame 0 builds it; frames 3 and 6 re-raise and refresh it.
    assert calls["n"] == 3


def test_a_failed_mask_refresh_keeps_the_previous_one(rec_session, monkeypatch):
    """The refresh is opportunistic. If it raises mid-clip the clip must carry
    on with the mask it already has, not lose framing."""
    real = session_mod.su.device_art_canvas_mask
    calls = {"n": 0}

    def first_only(img, box, *a, **kw):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("refresh exploded")
        return real(img, box, *a, **kw)

    monkeypatch.setattr(session_mod.su, "device_art_canvas_mask", first_only)
    res = _run_record(rec_session, [device_art_frame() for _ in range(5)],
                      refocus_every=2)
    assert res["ok"] is True and res["framed"] is True and res["frames"] == 5
