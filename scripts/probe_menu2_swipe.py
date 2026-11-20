#!/usr/bin/env python3
"""Probe: which synthetic gesture scrolls a Menu2 in the macOS simulator (F2).

Finding F2: on venu3 a `swipe up` act does not scroll a Menu2 (taps land, the
VM is not blocked, and the same swipe works on a custom View). This script
loads an app, walks to the Menu2 with the given nav steps, and tries one
gesture variant at a time, judging each with the SAME change check
`SimSession.act` uses (`_wait_stable` baseline, `_input_changed` polled for
`--settle` seconds).

What ciqkit sends today (capture/screenshot_utils.py `_drag`): pyautogui
mouseDown, then 12 `pyautogui.moveTo` calls -- which on macOS post
kCGEventMouseMoved, NOT kCGEventLeftMouseDragged -- all within ~1 ms (their
0.03 s duration is under pyautogui's MINIMUM_DURATION, and ciqkit sets PAUSE
to 0), then mouseUp 50 ms later. The variants below separate the factors:
event type (moved vs dragged) x timing, plus start point, pre-move hold and
scroll-wheel events.

macOS only. Drives SimSession DIRECTLY, so run it with the broker stopped
(`ciqkit sim stop` or wait for its idle exit); it takes the machine-wide
`sim` lease like the broker does, so it waits rather than fight one.

    python3 scripts/probe_menu2_swipe.py --device venu3 --prg bin/app.prg \\
        --steps '[{"kind":"tap","x":0.5,"y":0.5},{"kind":"hold","x":0.5,"y":0.5}]'

    --only dragged_slow,wheel_line_down   run a subset (see --list)
    --no-reset                            do not reload + re-navigate between
                                          variants (faster; results then depend
                                          on order)
    --record-manual 10                    afterwards, listen 10 s while YOU drag
                                          the menu with the real mouse, and print
                                          the event types/timing it produced

Prints one JSON line per variant to stdout, then a summary line. Exit 0 if a
non-control variant scrolled, 1 if none did, 2 on a setup error. Frames go to
--out (default: a temp dir) as <variant>-before.png / -after.png -- look at
them before trusting a "no change".
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Probe THIS checkout's code even when another ciqkit is installed.
sys.path.insert(0, os.path.join(ROOT, "src"))

log = logging.getLogger("probe_menu2_swipe")

# ── raw Quartz input ─────────────────────────────────────────────────────────


def _q():
    import Quartz  # pyobjc-framework-Quartz, part of the `sim` extra on macOS
    return Quartz


def _post_mouse(kind, x, y, click_state=1):
    Q = _q()
    ev = Q.CGEventCreateMouseEvent(None, kind, (x, y), Q.kCGMouseButtonLeft)
    Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventClickState, click_state)
    Q.CGEventPost(Q.kCGHIDEventTap, ev)


def cg_drag(c, a, b, *, move="dragged", steps=12, dur_s=0.0, hold_s=0.12,
            end_hold_s=0.05):
    """Press at display fraction `a`, move to `b` in `steps` events spread over
    `dur_s`, hold `end_hold_s`, release. `move` picks the event type of the
    intermediate events: "dragged" (kCGEventLeftMouseDragged, what a real
    mouse drag produces) or "moved" (kCGEventMouseMoved, what pyautogui.moveTo
    produces -- ciqkit's current gesture)."""
    from ciqkit.capture import screenshot_utils as su
    Q = _q()
    sx, sy = su.content_point(c, *a)
    ex, ey = su.content_point(c, *b)
    su.focus_window(c)
    _post_mouse(Q.kCGEventMouseMoved, sx, sy, 0)
    time.sleep(0.05)
    t0 = time.monotonic()
    _post_mouse(Q.kCGEventLeftMouseDown, sx, sy)
    time.sleep(hold_s)
    kind = Q.kCGEventLeftMouseDragged if move == "dragged" else Q.kCGEventMouseMoved
    for i in range(1, steps + 1):
        t = i / float(steps)
        _post_mouse(kind, int(sx + (ex - sx) * t), int(sy + (ey - sy) * t),
                    1 if move == "dragged" else 0)
        if dur_s:
            time.sleep(dur_s / steps)
    time.sleep(end_hold_s)
    _post_mouse(Q.kCGEventLeftMouseUp, ex, ey)
    return {"from_xy": [sx, sy], "to_xy": [ex, ey],
            "press_s": round(time.monotonic() - t0, 3)}


def cg_wheel(c, at=(0.5, 0.5), *, unit="line", delta=-3, count=1, gap_s=0.05):
    """Scroll-wheel events with the pointer over display fraction `at`.
    Negative delta = wheel toward the user = content scrolls up (later items
    come into view) in macOS's raw convention; both signs are probed because
    'natural scrolling' and the app may each flip it."""
    from ciqkit.capture import screenshot_utils as su
    Q = _q()
    x, y = su.content_point(c, *at)
    su.focus_window(c)
    # The capture parks the pointer outside the window before every grab, and
    # wheel events go to whatever is under the pointer: put it back first.
    _post_mouse(Q.kCGEventMouseMoved, x, y, 0)
    time.sleep(0.1)
    u = Q.kCGScrollEventUnitLine if unit == "line" else Q.kCGScrollEventUnitPixel
    for _ in range(count):
        ev = Q.CGEventCreateScrollWheelEvent(None, u, 1, int(delta))
        Q.CGEventSetLocation(ev, (x, y))
        Q.CGEventPost(Q.kCGHIDEventTap, ev)
        time.sleep(gap_s)
    return {"at_xy": [x, y]}


# ── the variants ─────────────────────────────────────────────────────────────
# ciqkit's default centred flick runs 0.71 -> 0.29 of the display height
# (span 0.84 of the width, half of it used, centred). Keep that path unless
# the variant is ABOUT the path, so only one factor changes at a time.
A, B = (0.5, 0.71), (0.5, 0.29)


def _variants():
    from ciqkit.capture import screenshot_utils as su
    return [
        # name, what it isolates, fn(canvas) -> dict, is_control
        ("noop", "no input: does the screen change by itself (false positive)?",
         lambda c: {}, True),
        ("ciqkit_swipe_up", "today's `swipe up` (pm_swipe_frac): reproduces F2",
         lambda c: su.pm_swipe_frac(c, direction="up", span=0.84, delay=0) or {}, True),
        ("ciqkit_swipe_from", "today's `swipe from:[0.5,0.85] dist:0.6`",
         lambda c: su.pm_swipe_from_frac(c, 0.5, 0.85, direction="up", dist=0.6,
                                         delay=0) or {}, True),
        ("moved_burst", "Quartz replica of today's gesture (moved, burst)",
         lambda c: cg_drag(c, A, B, move="moved", steps=12, dur_s=0.0), True),
        ("moved_slow", "moved events, spread over 0.6 s: does timing alone fix it?",
         lambda c: cg_drag(c, A, B, move="moved", steps=40, dur_s=0.6), False),
        ("dragged_burst", "dragged events, same burst timing: does type alone fix it?",
         lambda c: cg_drag(c, A, B, move="dragged", steps=12, dur_s=0.0), False),
        ("dragged_fast", "dragged, 12 events over 0.15 s (a flick)",
         lambda c: cg_drag(c, A, B, move="dragged", steps=12, dur_s=0.15), False),
        ("dragged_medium", "dragged, 30 events over 0.4 s",
         lambda c: cg_drag(c, A, B, move="dragged", steps=30, dur_s=0.4), False),
        ("dragged_slow", "dragged, 60 events over 1.0 s, 0.3 s still before release",
         lambda c: cg_drag(c, A, B, move="dragged", steps=60, dur_s=1.0,
                           end_hold_s=0.3), False),
        ("dragged_long_path", "dragged 0.85 -> 0.15, 40 events over 0.5 s",
         lambda c: cg_drag(c, (0.5, 0.85), (0.5, 0.15), move="dragged", steps=40,
                           dur_s=0.5), False),
        ("dragged_upper", "dragged 0.55 -> 0.20 (start well inside the list)",
         lambda c: cg_drag(c, (0.5, 0.55), (0.5, 0.20), move="dragged", steps=30,
                           dur_s=0.3), False),
        ("dragged_short", "dragged 0.60 -> 0.45: a follow-the-finger scroll of ~1 row",
         lambda c: cg_drag(c, (0.5, 0.60), (0.5, 0.45), move="dragged", steps=20,
                           dur_s=0.3, end_hold_s=0.3), False),
        ("dragged_long_hold", "0.5 s press before moving, then dragged over 0.4 s",
         lambda c: cg_drag(c, A, B, move="dragged", steps=30, dur_s=0.4,
                           hold_s=0.5), False),
        ("wheel_line_down", "scroll wheel, 3 lines, negative delta",
         lambda c: cg_wheel(c, unit="line", delta=-3), False),
        ("wheel_line_up", "scroll wheel, 3 lines, positive delta",
         lambda c: cg_wheel(c, unit="line", delta=3), False),
        ("wheel_line_ticks", "scroll wheel, 8 single-line ticks, negative",
         lambda c: cg_wheel(c, unit="line", delta=-1, count=8, gap_s=0.08), False),
        ("wheel_pixel_down", "trackpad-like: 15 x -12 px, negative",
         lambda c: cg_wheel(c, unit="pixel", delta=-12, count=15, gap_s=0.016), False),
        ("wheel_pixel_up", "trackpad-like: 15 x +12 px, positive",
         lambda c: cg_wheel(c, unit="pixel", delta=12, count=15, gap_s=0.016), False),
    ]


# ── one trial ────────────────────────────────────────────────────────────────


def _detail_hits(s, before, after) -> int:
    """How many detail cells moved by details_differ's 24 levels (3 = changed)."""
    a, b = s._frame_detail(before), s._frame_detail(after)
    if not a or not b or len(a) != len(b):
        return -1
    return sum(1 for x, y in zip(a, b, strict=True) if abs(x - y) >= 24.0)


def trial(s, name, fn, settle_s, out_dir) -> dict:
    """Mirror SimSession.act's order: dismiss modals, stable baseline, note the
    display, rebind + restore the window, input, then poll the change check.
    Private session methods on purpose: the point is the exact check act uses."""
    from ciqkit.capture import screenshot_utils as su
    su.dismiss_modal_dialogs(verbose=False)
    before = s._wait_stable()
    s._note_display(before)
    s._rebind()
    su.restore_window(s._hwnd)
    time.sleep(0.2)
    s._rebind()
    t0 = time.monotonic()
    info = fn(s._canvas) or {}
    sent_s = time.monotonic() - t0
    deadline = time.monotonic() + settle_s
    first_change = None
    after = before
    while True:
        after = s._capture()
        if s._input_changed(before, after):
            if first_change is None:
                first_change = round(time.monotonic() - t0, 2)
        if time.monotonic() >= deadline:
            break
        time.sleep(s.SETTLE_POLL_S)
    # One more look after the window: a list that scrolled and sprang back
    # (rubber-band) counts as "changed at some poll" but not "changed at end".
    time.sleep(0.5)
    final = s._capture()
    try:
        before.save(os.path.join(out_dir, f"{name}-before.png"))
        final.save(os.path.join(out_dir, f"{name}-after.png"))
    except Exception as exc:  # frames are a convenience, never the verdict
        log.warning("could not save frames for %s: %s", name, exc)
    coarse = su.signatures_differ(s._face_sig(before), s._face_sig(final))
    return {"changed": first_change is not None,
            "first_change_s": first_change,
            "changed_at_end": s._input_changed(before, final),
            "coarse_at_end": bool(coarse),
            "detail_cells_at_end": _detail_hits(s, before, final),
            "sent_s": round(sent_s, 3), **info}


# ── manual recording ─────────────────────────────────────────────────────────


def record_manual(seconds: float) -> dict:
    """Listen-only event tap while the user drags the menu with the real mouse:
    which event types, how many, over how long. Needs Input Monitoring for the
    terminal. This is the ground truth to replay if no variant scrolls."""
    Q = _q()
    names = {Q.kCGEventLeftMouseDown: "down", Q.kCGEventLeftMouseUp: "up",
             Q.kCGEventLeftMouseDragged: "dragged", Q.kCGEventMouseMoved: "moved",
             Q.kCGEventScrollWheel: "wheel"}
    mask = 0
    for k in names:
        mask |= Q.CGEventMaskBit(k)
    seen = []

    def cb(_proxy, etype, ev, _refcon):
        loc = Q.CGEventGetLocation(ev)
        seen.append((time.monotonic(), names.get(etype, str(etype)),
                     (round(loc.x), round(loc.y))))
        return ev

    tap = Q.CGEventTapCreate(Q.kCGSessionEventTap, Q.kCGHeadInsertEventTap,
                             Q.kCGEventTapOptionListenOnly, mask, cb, None)
    if not tap:
        return {"ok": False, "reason": "event tap refused: grant Input Monitoring "
                                       "to this terminal and re-run"}
    src = Q.CFMachPortCreateRunLoopSource(None, tap, 0)
    Q.CFRunLoopAddSource(Q.CFRunLoopGetCurrent(), src, Q.kCFRunLoopCommonModes)
    Q.CGEventTapEnable(tap, True)
    print(f"# drag the menu with the mouse now ({seconds:.0f} s)...",
          file=sys.stderr, flush=True)
    Q.CFRunLoopRunInMode(Q.kCFRunLoopDefaultMode, seconds, False)
    Q.CGEventTapEnable(tap, False)
    # Summarise each press: down .. up, the event types between and the time.
    presses, cur = [], None
    for t, kind, xy in seen:
        if kind == "down":
            cur = {"t0": t, "from": xy, "counts": {}}
        elif cur is not None and kind == "up":
            presses.append({"from": cur["from"], "to": xy,
                            "duration_s": round(t - cur["t0"], 3),
                            "between": cur["counts"]})
            cur = None
        elif cur is not None:
            cur["counts"][kind] = cur["counts"].get(kind, 0) + 1
    wheels = sum(1 for _, k, _ in seen if k == "wheel")
    return {"ok": True, "presses": presses, "wheel_events": wheels}


# ── main ─────────────────────────────────────────────────────────────────────


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--device", default="venu3")
    ap.add_argument("--prg", required=True, help="built .prg to load")
    ap.add_argument("--app", help="app id for the session (default: .prg basename)")
    ap.add_argument("--project", help="project dir (default: the .prg's grandparent)")
    ap.add_argument("--steps", default="[]",
                    help="JSON list of nav steps (the `reach` step format) from the "
                         "app's first screen to the Menu2")
    ap.add_argument("--steps-file", help="read the nav steps from this JSON file")
    ap.add_argument("--only", help="comma-separated subset of variant names")
    ap.add_argument("--list", action="store_true", help="list the variants and exit")
    ap.add_argument("--no-reset", action="store_true",
                    help="do not reload + re-navigate before each variant")
    ap.add_argument("--settle", type=float, default=None,
                    help="seconds to poll for a change (default: SimSession.SETTLE_S)")
    ap.add_argument("--out", help="directory for frames + probe.jsonl")
    ap.add_argument("--record-manual", type=float, metavar="SECONDS", default=0.0,
                    help="after the variants, record a real mouse drag for this long")
    a = ap.parse_args(argv)

    if a.list:
        for name, what, _fn, ctl in _variants():
            print(f"{name:20s} {'(control) ' if ctl else ''}{what}")
        return 0
    if sys.platform != "darwin":
        print("macOS only: the variants post Quartz events", file=sys.stderr)
        return 2
    logging.basicConfig(level=os.environ.get("CIQKIT_LOG_LEVEL", "INFO"),
                        stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    from ciqkit import leases
    from ciqkit.sim.protocol import step_errors
    from ciqkit.sim.session import SimSession

    steps = json.load(open(a.steps_file)) if a.steps_file else json.loads(a.steps)
    errs = [e for i, st in enumerate(steps) for e in step_errors(st, f"step {i + 1}")]
    if errs:
        print("bad --steps: " + "; ".join(errs), file=sys.stderr)
        return 2
    variants = _variants()
    if a.only:
        want = {x.strip() for x in a.only.split(",") if x.strip()}
        unknown = want - {v[0] for v in variants}
        if unknown:
            print(f"unknown variant(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        variants = [v for v in variants if v[0] in want]

    prg = os.path.abspath(a.prg)
    app = a.app or os.path.splitext(os.path.basename(prg))[0]
    out_dir = a.out or tempfile.mkdtemp(prefix="probe-menu2-")
    os.makedirs(out_dir, exist_ok=True)
    jsonl = open(os.path.join(out_dir, "probe.jsonl"), "a")
    print(f"# frames and probe.jsonl -> {out_dir}", file=sys.stderr, flush=True)

    def emit(obj):
        line = json.dumps(obj)
        print(line, flush=True)
        jsonl.write(line + "\n")
        jsonl.flush()

    s = SimSession(out_dir=out_dir)
    settle = a.settle if a.settle is not None else s.SETTLE_S

    def reset(force: bool) -> str | None:
        res = s.load(app, prg, a.device, project=a.project, force=force)
        if not res.get("ok"):
            return f"load failed: {res}"
        if steps:
            r = s.reach(steps)
            if not r.get("ok"):
                return f"nav failed: {r.get('error') or r}"
        return None

    results = []
    with leases.lease("sim", timeout=30, meta={"holder": "probe_menu2_swipe"}):
        if a.no_reset:
            err = reset(force=True)
            if err:
                emit({"setup": True, "ok": False, "reason": err})
                return 2
        for name, what, fn, control in variants:
            row = {"variant": name, "control": control, "what": what,
                   "device": a.device}
            try:
                err = None if a.no_reset else reset(force=True)
                if err:
                    row.update(ok=False, reason=err)
                else:
                    row.update(ok=True, **trial(s, name, fn, settle, out_dir))
            except Exception as exc:  # one broken variant must not end the probe
                log.exception("variant %s raised", name)
                row.update(ok=False, reason=f"{type(exc).__name__}: {exc}")
            emit(row)
            results.append(row)
        manual = record_manual(a.record_manual) if a.record_manual > 0 else None

    def names(pred):
        return [r["variant"] for r in results if pred(r)]
    drift = any(r["variant"] == "noop" and r.get("changed") for r in results)
    scrolled = names(lambda r: r.get("ok") and r.get("changed") and not r["control"])
    summary = {
        "summary": True, "device": a.device, "out": out_dir,
        # noop changing means every "changed" below is suspect.
        "drift": drift,
        "controls_changed": names(lambda r: r.get("ok") and r.get("changed")
                                  and r["control"]),
        "scrolled": scrolled,
        "sprang_back": names(lambda r: r.get("changed") and not r.get("changed_at_end")),
        "no_change": names(lambda r: r.get("ok") and not r.get("changed")),
        "errors": names(lambda r: not r.get("ok")),
    }
    if manual is not None:
        summary["manual"] = manual
    emit(summary)
    return 0 if scrolled and not drift else 1


if __name__ == "__main__":
    sys.exit(main())
