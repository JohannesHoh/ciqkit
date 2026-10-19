"""
Connect IQ simulator window capture, watch-face detection and input primitives.

This is the low-level engine under :mod:`ciqkit.sim`; most callers should use
the ``ciqkit`` CLI or :class:`ciqkit.sim.session.SimSession` instead of calling
into here directly.

Platforms: macOS (window discovery and input through AppleScript/System
Events, capture through Quartz/``screencapture``) and Windows (Win32 through
ctypes: ``PrintWindow`` capture, SendInput for gestures -- see the Windows
notes at :func:`capture_window` and :func:`focus_window` for what the
simulator does and does not accept there). Linux is not supported: Garmin does
not ship a Linux simulator.

Direct use::

    from ciqkit.capture import screenshot_utils as su
    su.init(sdk_path, project_dir)
    hwnd = su.ensure_simulator()
    img, scale = su.capture_window(hwnd)

What ciqkit actually calls, grouped:

  setup      init(sdk, project, out_dir), sdk_bin(name)
  simulator  ensure_simulator(), _restart_simulator(), _kill_orphan_monkeydo(),
             find_sim_window(), find_canvas(hwnd), restore_window(hwnd),
             dismiss_modal_dialogs(verbose)
  capture    capture_window(hwnd) -> (PIL image in logical px, dpi scale)
  geometry   find_display_circle(img, device_id, device_res) -> (cx, cy, radius)
             -- the detector callers want; device-art aware, falls back to
             find_watch_face_auto(img, device_res),
             crop_face, crop_watch_body, face_region_signature,
             signatures_differ, frame_detail_signature, details_differ,
             _looks_like_app_loaded
  framing    make_sdk_framed_screenshot, make_framed_screenshot,
             frame_device_art_capture
  input      pm_click, pm_swipe, phys_key

Other module-level functions exist and are not on that path; they are legacy
from the batch-capture script this was extracted from and are not exercised by
ciqkit's tests. Treat them as unsupported.
"""

import logging
import os
import sys
import time
import subprocess
import tempfile

import numpy as np
from PIL import Image

# ── moved out, re-exported here ───────────────────────────────────────────────
# This module used to be one 1600-line file mixing five unrelated concerns.
# The two PURE-IMAGING halves now live next door, which is what makes them
# testable headless; everything left here needs a real window, a subprocess or
# a platform API. Callers keep using `screenshot_utils` as the single entry
# point -- these names are part of its public surface and are re-exported, not
# relocated.
from . import simwindows as _sw
from .geometry import (              # noqa: F401  -- re-exported API
    DARK_THRESH, MIN_DISPLAY_WIDTH,
    _corner_brightness, _looks_like_app_loaded,
    crop_face, crop_watch_body, details_differ, frame_detail_signature,
    face_fit_score, face_region_signature,
    find_watch_face, find_watch_face_auto, find_watch_face_chrome,
    signatures_differ,
)
from .framing import (               # noqa: F401  -- re-exported API
    STORE_BG_COLOR, STORE_SCREENSHOT_SIZE,
    _autocrop_native_to_screen, _detect_sdk_screen_rect, _sdk_device_png_path,
    apply_device_art_frame, autocrop_screen_box, detect_device_art_geometry,
    device_art_canvas_mask, device_art_screen_circle, find_display_circle,
    looks_like_app_loaded, sim_display_box,
    sim_device_keys, sim_device_point, sim_button_point,
    frame_device_art_capture, make_framed_screenshot, make_sdk_framed_screenshot,
)

# A library must not write to stdout: callers parse it, and the MCP server
# speaks JSON-RPC over it. Diagnostics go through logging, silent by default;
# `ciqkit doctor` and the broker daemon turn it up.
log = logging.getLogger("ciqkit.capture")


class SimulatorLaunchError(RuntimeError):
    """The simulator could not be started (missing SDK, bundle, or timeout).

    A raised exception, NOT ``sys.exit``. This module is library code called
    from the broker's scheduler thread, and ``SystemExit`` derives from
    ``BaseException``: an ``except Exception`` guard -- which is what every
    caller sensibly writes -- lets it through, and inside a thread that kills
    the thread silently. The broker's scheduler dying that way leaves a daemon
    that still accepts jobs and never runs one. Same reasoning as "never write
    to stdout from library code": a library reports failures in-band.
    """


class SimulatorInputError(RuntimeError):
    """Input cannot reach the simulator at all (e.g. a locked Windows session).

    Raised instead of firing the gesture into the void: an unconfirmed tap is
    retried, and three silent retries of something that can never land only
    turn a one-line cause into a minute-long "the screen did not change".
    """


class _LazyPyAutoGUI:
    """Import pyautogui on first use, not at module import.

    pyautogui pulls in platform GUI bindings and fails outright on a headless
    box. Importing it eagerly would make `import ciqkit.sim` -- and therefore
    the broker's unit tests, which never touch a screen -- impossible to run in
    CI. Deferring it means only the code paths that actually move the mouse
    need it, and the error names the extra to install.
    """

    _mod = None

    def _load(self):
        if self._mod is None:
            try:
                import pyautogui as _p
            except Exception as exc:  # ImportError, or a GUI-less DISPLAY error
                raise RuntimeError(
                    "pyautogui is required to send input to the simulator. "
                    "Install it with:  pip install 'ciqkit[sim]'"
                ) from exc
            _p.PAUSE = 0.0        # callers manage delays explicitly
            _p.FAILSAFE = False   # automated capture; coords may exceed screen
            self._mod = _p
        return self._mod

    def __getattr__(self, name):
        return getattr(self._load(), name)


pyautogui = _LazyPyAutoGUI()

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------
IS_WIN = sys.platform == 'win32'
IS_MAC = sys.platform == 'darwin'

if IS_WIN:
    import ctypes
    import ctypes.wintypes as wt
    # Per-monitor DPI aware, BEFORE anything asks for a coordinate: every
    # rect this module reads and every point it clicks is then in physical
    # pixels, which is the one coordinate system SendInput and the screen
    # agree on. (pyautogui would otherwise make the process merely
    # system-aware on import, and taps would drift on any monitor whose
    # scale differs from the primary's.)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Module-level state (set via init)
# ---------------------------------------------------------------------------
# The SDK path and project root the simulator is launched with. Module-level
# because the whole module is a singleton driver for one GUI process, and the
# broker's single-actor invariant is what makes that safe -- but it IS hidden
# state with an ordering contract, so the readers below check it rather than
# quietly producing a nonsense path. STORE_SCREENSHOT_SIZE / STORE_BG_COLOR now
# live in .framing and are re-exported above.
_sdk: str = ""
_project: str = ""


def init(sdk: str, project: str) -> None:
    """Set the SDK path and the project root used to launch the simulator."""
    global _sdk, _project
    _sdk = sdk
    _project = project

# ---------------------------------------------------------------------------
# Frame-size helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SDK discovery
# ---------------------------------------------------------------------------


def sdk_bin(name: str) -> str:
    """Full path to an SDK binary. Adds the .bat suffix on Windows.

    Refuses to answer before :func:`init`. With ``_sdk`` still empty this used
    to join to ``/bin/monkeydo`` -- an absolute path to a file that does not
    exist, or worse, on some systems does. The caller then got a
    FileNotFoundError from a Popen three frames away, or launched the wrong
    binary; naming the missing setup here is the difference between a
    five-second fix and a hunt.
    """
    if not _sdk:
        raise SimulatorLaunchError(
            f"cannot resolve the SDK binary {name!r}: screenshot_utils.init() "
            f"has not been called, so no Connect IQ SDK path is known. Run "
            f"`ciqkit doctor` to check CIQ_HOME.")
    suffix = ".bat" if IS_WIN else ""
    return os.path.join(_sdk, "bin", name + suffix)


# ---------------------------------------------------------------------------
# Win32 prototypes & structures  (Windows only)
# ---------------------------------------------------------------------------
if IS_WIN:
    # Declared prototypes, not bare `ctypes.windll` calls: an HWND or HDC is
    # pointer-sized, and ctypes' default int conversion overflows (or worse,
    # truncates) one on 64-bit Windows.
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left",   ctypes.c_long), ("top",    ctypes.c_long),
            ("right",  ctypes.c_long), ("bottom", ctypes.c_long),
        ]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT), ("rcWork", RECT),
                    ("dwFlags", wt.DWORD)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize",          ctypes.c_uint32), ("biWidth",         ctypes.c_int32),
            ("biHeight",        ctypes.c_int32),  ("biPlanes",        ctypes.c_uint16),
            ("biBitCount",      ctypes.c_uint16), ("biCompression",   ctypes.c_uint32),
            ("biSizeImage",     ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
            ("biYPelsPerMeter", ctypes.c_int32),  ("biClrUsed",       ctypes.c_uint32),
            ("biClrImportant",  ctypes.c_uint32),
        ]

    for _name, _res, _args in (
        ("EnumWindows", wt.BOOL, (WNDENUMPROC, wt.LPARAM)),
        ("EnumChildWindows", wt.BOOL, (wt.HWND, WNDENUMPROC, wt.LPARAM)),
        ("GetWindowTextW", ctypes.c_int, (wt.HWND, wt.LPWSTR, ctypes.c_int)),
        ("GetWindowTextLengthW", ctypes.c_int, (wt.HWND,)),
        ("GetClassNameW", ctypes.c_int, (wt.HWND, wt.LPWSTR, ctypes.c_int)),
        ("GetWindowThreadProcessId", wt.DWORD, (wt.HWND, ctypes.POINTER(wt.DWORD))),
        ("IsWindow", wt.BOOL, (wt.HWND,)),
        ("IsWindowVisible", wt.BOOL, (wt.HWND,)),
        ("IsIconic", wt.BOOL, (wt.HWND,)),
        ("GetWindowRect", wt.BOOL, (wt.HWND, ctypes.POINTER(RECT))),
        ("ShowWindow", wt.BOOL, (wt.HWND, ctypes.c_int)),
        ("SetForegroundWindow", wt.BOOL, (wt.HWND,)),
        ("GetForegroundWindow", wt.HWND, ()),
        ("BringWindowToTop", wt.BOOL, (wt.HWND,)),
        ("AttachThreadInput", wt.BOOL, (wt.DWORD, wt.DWORD, wt.BOOL)),
        ("SetFocus", wt.HWND, (wt.HWND,)),
        ("PostMessageW", wt.BOOL, (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)),
        ("SetWindowPos", wt.BOOL, (wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.UINT)),
        ("MonitorFromWindow", wt.HMONITOR, (wt.HWND, wt.DWORD)),
        ("GetMonitorInfoW", wt.BOOL, (wt.HMONITOR, ctypes.POINTER(MONITORINFO))),
        ("GetDC", wt.HDC, (wt.HWND,)),
        ("ReleaseDC", ctypes.c_int, (wt.HWND, wt.HDC)),
        ("PrintWindow", wt.BOOL, (wt.HWND, wt.HDC, wt.UINT)),
    ):
        _fn = getattr(user32, _name)
        _fn.restype, _fn.argtypes = _res, _args
    try:   # Windows 10 1607+
        user32.GetDpiForWindow.restype = wt.UINT
        user32.GetDpiForWindow.argtypes = (wt.HWND,)
    except AttributeError:
        pass
    for _name, _res, _args in (
        ("CreateCompatibleDC", wt.HDC, (wt.HDC,)),
        ("CreateCompatibleBitmap", wt.HBITMAP, (wt.HDC, ctypes.c_int, ctypes.c_int)),
        ("SelectObject", wt.HGDIOBJ, (wt.HDC, wt.HGDIOBJ)),
        ("GetDIBits", ctypes.c_int, (wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT,
                                     ctypes.c_void_p, ctypes.c_void_p, wt.UINT)),
        ("DeleteObject", wt.BOOL, (wt.HGDIOBJ,)),
        ("DeleteDC", wt.BOOL, (wt.HDC,)),
    ):
        _fn = getattr(gdi32, _name)
        _fn.restype, _fn.argtypes = _res, _args
    _kernel32.GetCurrentThreadId.restype = wt.DWORD
else:
    user32 = None
    gdi32  = None


# ---------------------------------------------------------------------------
# Cross-platform window/rect helpers
# ---------------------------------------------------------------------------

class _Rect:
    """Lightweight RECT substitute used on non-Windows platforms."""
    __slots__ = ("left", "top", "right", "bottom")

    def __init__(self, left: int, top: int, right: int, bottom: int):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


class _SimWindow:
    """Opaque simulator window handle.

    Carries the window rect AND a capture-derived geometry side-channel:

      content_left / content_top -- offset of the simulator's display
          content inside the captured screenshot (i.e. how much OS chrome
          and title bar capture_window trimmed off);
      retina_scale -- captured-image pixels per screen pixel of that capture
          (2.0 on a Retina Mac; on Windows below 1.0 when the DPI-unaware
          simulator is drawn scaled up, e.g. 0.5 at 200 %).

    Those are written by :func:`capture_window` and read by
    :func:`pm_click` / :func:`pm_swipe` to map image coordinates back to
    screen coordinates. That makes them a TEMPORAL contract, not just
    fields: they describe one particular capture of one particular window
    position, and nothing in the type says so. `geometry_captured` records
    whether they were ever filled in, so a caller that clicks on a handle
    straight from find_sim_window() -- where they are still at their
    neutral defaults -- gets a warning instead of silently-shifted taps.

    `display_box` is optional and Windows-only today: the watch display's
    bounding box inside the content image, which a session records after it
    has found the face, so a gesture fraction lands on the SCREEN rather than
    on the device art around it (see :func:`content_point`).
    """
    __slots__ = ("left", "top", "width", "height",
                 "content_left", "content_top", "content_w", "content_h",
                 "retina_scale", "geometry_captured", "display_box")

    def __init__(self, left: int, top: int, width: int, height: int):
        self.left = left
        self.top = top
        self.width = width
        self.height = height
        # Neutral defaults: no offset, no scaling, unknown content size.
        # Correct only until a capture says otherwise -- see
        # geometry_captured.
        self.content_left = 0
        self.content_top  = 0
        self.content_w    = 0
        self.content_h    = 0
        self.retina_scale = 1.0
        self.geometry_captured = False
        self.display_box = None

    def rect(self):
        return (self.left, self.top, self.width, self.height)


# The macOS name, kept: it is what the handle was called while macOS was the
# only platform with one, and callers and tests still use it.
_MacWindow = _SimWindow


class _WinWindow(_SimWindow):
    """A Windows simulator window: the handle plus what it was found from.

    ``panel`` is the ``wxWindowNR`` child titled "panel" that the simulator
    draws the device in. It is NOT a fixed part of the window -- it only
    exists once a device is loaded and is RE-CREATED by every push, which is
    why every gesture re-resolves it (via _rebind -> find_canvas) instead of
    caching it. Keyboard input only reaches the watch while it has focus.
    """
    __slots__ = ("hwnd", "pid", "panel")

    def __init__(self, hwnd: int, pid: int, left: int, top: int, width: int,
                 height: int):
        super().__init__(left, top, width, height)
        self.hwnd = hwnd
        self.pid = pid
        self.panel = None

    def __str__(self):
        return f"{self.hwnd:#x}"

    def __bool__(self):
        return bool(self.hwnd)


# ---------------------------------------------------------------------------
# Window helpers
# ---------------------------------------------------------------------------

if IS_WIN:
    def _win_text(hwnd) -> str:
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def _win_class(hwnd) -> str:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def _win_pid(hwnd) -> int:
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def _win_rect(hwnd):
        r = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        return r

    def _win_children(hwnd) -> list:
        out = []

        def _cb(h, _):
            out.append(h)
            return True
        user32.EnumChildWindows(hwnd, WNDENUMPROC(_cb), 0)
        return out

    def _win_sim_pids() -> set:
        from .._winapi import pids_named
        return set(pids_named("simulator.exe"))

    def _hwnd(h) -> int:
        """A raw HWND from a _WinWindow or an int (legacy callers)."""
        return getattr(h, "hwnd", h)

    def _win_find_sim_window():
        """The simulator's main frame, or None.

        Matched on the PROCESS (simulator.exe in our own logon session) and
        the wx frame class, not on the title alone: "CIQ Simulator" in a title
        is also an Explorer folder, an editor tab or a browser page about the
        simulator, and whichever of those sorted first used to be the window
        every capture photographed and every click went to.
        """
        pids = _win_sim_pids()
        if not pids:
            return None
        found = []

        def _cb(h, _):
            if (user32.IsWindowVisible(h) and _win_pid(h) in pids
                    and _win_class(h) == "wxWindowNR"
                    and "ciq simulator" in _win_text(h).lower()):
                found.append(h)
            return True
        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        if not found:
            return None
        h = found[0]
        r = _win_rect(h)
        w = _WinWindow(h, _win_pid(h), r.left, r.top, r.right - r.left,
                       r.bottom - r.top)
        _restore_geometry(w)
        return w

    def _win_find_panel(hwnd):
        """The child the device is drawn in: a wxWindowNR titled "panel".

        Absent until a device is loaded; re-created by every push. Falls back
        to the largest visible wxWindowNR child if a future SDK renames it.
        """
        best, best_area = None, 0
        for c in _win_children(hwnd):
            if not user32.IsWindowVisible(c) or _win_class(c) != "wxWindowNR":
                continue
            if _win_text(c) == "panel":
                return c
            r = _win_rect(c)
            area = (r.right - r.left) * (r.bottom - r.top)
            if area > best_area:
                best, best_area = c, area
        return best

    def _win_dpi_ratio(hwnd) -> float:
        """Image pixels per physical pixel for a PrintWindow of ``hwnd``.

        The Connect IQ simulator is not DPI-aware. Windows draws it at 96 DPI
        and scales the result up on a high-DPI monitor, and PrintWindow hands
        back the UNSCALED drawing -- 0.5x the window's physical size at 200 %.
        That is the watch at its native pixel size, which is what a screenshot
        wants; it just has to be converted back for every click. A DPI-aware
        window would report the monitor's DPI and the ratio is 1.
        """
        try:
            win_dpi = user32.GetDpiForWindow(hwnd)
            mon = user32.MonitorFromWindow(hwnd, 2)   # MONITOR_DEFAULTTONEAREST
            mx, my = wt.UINT(), wt.UINT()
            if ctypes.windll.shcore.GetDpiForMonitor(mon, 0, ctypes.byref(mx),
                                                     ctypes.byref(my)) != 0:
                return 1.0
            if win_dpi and mx.value:
                return win_dpi / float(mx.value)
        except Exception:
            pass
        return 1.0

    def _win_work_area(hwnd):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        mon = user32.MonitorFromWindow(hwnd, 2)
        if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return None
        return mi.rcWork

    def _win_bring_into_view(win) -> None:
        """Move the window so the part a gesture aims at is on-screen.

        Real input only lands where the window is actually visible: the
        simulator restores its last size, which on a high-DPI laptop is
        routinely taller than the work area, and a tap aimed below the
        taskbar lands ON the taskbar. A pure move keeps every capture-derived
        offset valid (they are relative to the window), so the handle is
        updated in place and its cached geometry with it.
        """
        area = _win_work_area(win.hwnd)
        if area is None:
            return
        # What has to be visible: the display if we know where it is, else
        # the whole content area.
        s = win.retina_scale or 1.0
        if win.geometry_captured and win.display_box:
            x0, y0, x1, y1 = win.display_box
        elif win.geometry_captured and win.content_w:
            x0, y0, x1, y1 = 0, 0, win.content_w, win.content_h
        else:
            return
        top = win.top + int((win.content_top + y0) / s)
        bottom = win.top + int((win.content_top + y1) / s)
        left = win.left + int((win.content_left + x0) / s)
        right = win.left + int((win.content_left + x1) / s)
        dx = dy = 0
        if bottom > area.bottom:
            dy = area.bottom - bottom
        if top + dy < area.top:
            dy = area.top - top
        if right > area.right:
            dx = area.right - right
        if left + dx < area.left:
            dx = area.left - left
        if not dx and not dy:
            return
        SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x4, 0x10
        if user32.SetWindowPos(win.hwnd, None, win.left + dx, win.top + dy, 0, 0,
                               SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE):
            log.info("moved the simulator window by (%d, %d) so the watch "
                     "display is on-screen for input", dx, dy)
            win.left += dx
            win.top += dy
            if win.geometry_captured:
                _remember_geometry(win)
            time.sleep(0.15)

    def _win_focus(win) -> None:
        """Foreground the simulator AND give its device panel keyboard focus.

        Both halves were measured against the SDK 8.4 simulator:

          * it ignores PostMessage'd mouse and key messages outright (the
            pre-existing Windows path relied on them, and on a canvas child
            that the current simulator does not even have), so gestures are
            real SendInput, which goes to whatever is foreground;
          * keys only reach the watch while the "panel" child has focus --
            with the frame foreground but focus elsewhere, Enter and Esc are
            silently dropped.

        Getting the foreground at all is the hard part. Windows' foreground
        lock refuses SetForegroundWindow to a process the user is not
        interacting with -- which is exactly what the broker is, a detached
        daemon -- and every documented workaround (attaching to the
        foreground thread's input queue, SwitchToThisWindow, the ALT-key
        trick) was measured failing from it. Two things ARE allowed: changing
        z-order, and real user input, which always activates what it clicks.
        So the fallback lifts the simulator to the top for a moment and
        clicks a blank strip of its device panel -- the white canvas beside
        the watch, which the simulator ignores -- and drops it back.

        A locked session discards every synthesized event, so that is
        checked first and raised as SimulatorInputError rather than sent.
        """
        from .. import _winapi
        if _winapi.session_locked():
            raise SimulatorInputError(
                "the Windows session is locked, so taps, swipes and keys cannot "
                "reach the simulator (screenshots still work). Unlock it, or "
                "keep it from locking while gestures run.")
        hwnd = win.hwnd
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        panel = getattr(win, "panel", None) or _win_find_panel(hwnd)
        _win_attach_focus(hwnd, panel)
        if user32.GetForegroundWindow() != hwnd:
            _win_click_to_front(win)
            _win_attach_focus(hwnd, panel)
        if user32.GetForegroundWindow() != hwnd:
            raise SimulatorInputError(
                "could not bring the simulator to the foreground, so input "
                "would go to whatever window is on top instead. Is another "
                "window set always-on-top over it?")

    def _win_attach_focus(hwnd, panel) -> None:
        """The polite way: SetForegroundWindow + SetFocus(panel) while
        attached to the foreground thread's and the simulator's input
        queues. Enough when we have foreground rights, and the only way to
        put keyboard focus on a child window of another process."""
        me = _kernel32.GetCurrentThreadId()
        sim_tid = user32.GetWindowThreadProcessId(hwnd, None)
        fg = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = []
        for tid in {sim_tid, fg_tid} - {0, me}:
            if user32.AttachThreadInput(me, tid, True):
                attached.append(tid)
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            if panel:
                user32.SetFocus(panel)
        finally:
            for tid in attached:
                user32.AttachThreadInput(me, tid, False)
        time.sleep(0.15)

    def _win_click_to_front(win) -> None:
        """Topmost for a moment, then a real click on blank panel canvas.

        The click goes to the panel's left margin at the height of the
        display: inside the window (so it activates it and focuses the
        panel), outside the device art (so the simulator does nothing with
        it), and on-screen whenever the display is, which _bring_into_view
        has already arranged.
        """
        hwnd = win.hwnd
        if not (win.geometry_captured and win.content_w):
            _win_capture(win)
        s = win.retina_scale or 1.0
        box = win.display_box
        mid_y = (box[1] + box[3]) // 2 if box else win.content_h // 2
        x = win.left + int((win.content_left + 4) / s)
        y = win.top + int((win.content_top + mid_y) / s)
        HWND_TOPMOST, HWND_NOTOPMOST = wt.HWND(-1), wt.HWND(-2)
        SWP_FLAGS = 0x1 | 0x2 | 0x10        # NOSIZE | NOMOVE | NOACTIVATE
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_FLAGS)
        try:
            time.sleep(0.1)
            pyautogui.click(x, y)
            time.sleep(0.15)
        finally:
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_FLAGS)
        log.debug("raised the simulator with a click at (%d, %d)", x, y)

    def _win_capture(win):
        """PrintWindow the frame, then crop it to the device panel.

        PrintWindow (PW_RENDERFULLCONTENT) reads the window's own drawing, so
        unlike a screen grab it is right even when the simulator is partly
        covered. The bitmap is allocated at the PHYSICAL size and cropped to
        the drawn part -- see _win_dpi_ratio. The crop to the panel takes off
        the title bar, the menu bar and the status bar: on macOS the dark-trim
        does the same job, but the Windows frame is light grey.
        """
        hwnd = win.hwnd
        r = _win_rect(hwnd)
        pw, ph = r.right - r.left, r.bottom - r.top
        win.left, win.top, win.width, win.height = r.left, r.top, pw, ph
        if pw <= 0 or ph <= 0:
            return Image.new("RGB", (1, 1), (0, 0, 0))
        s = _win_dpi_ratio(hwnd)

        hdc_win = user32.GetDC(hwnd)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_win, pw, ph)
        old = gdi32.SelectObject(hdc_mem, hbmp)
        try:
            PW_RENDERFULLCONTENT = 2
            ok = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)
            bih = BITMAPINFOHEADER()
            bih.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bih.biWidth, bih.biHeight = pw, -ph    # top-down
            bih.biPlanes, bih.biBitCount, bih.biCompression = 1, 32, 0
            buf = ctypes.create_string_buffer(pw * ph * 4)
            gdi32.GetDIBits(hdc_mem, hbmp, 0, ph, buf, ctypes.byref(bih), 0)
        finally:
            gdi32.SelectObject(hdc_mem, old)
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(hwnd, hdc_win)
        full = Image.frombuffer("RGBA", (pw, ph), buf, "raw", "BGRA", 0, 1).convert("RGB")
        lw, lh = max(1, round(pw * s)), max(1, round(ph * s))
        if not ok:
            # Fall back to the pixels on screen: correct only if unobscured,
            # which focus_window has just tried to make true.
            from PIL import ImageGrab
            full = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom),
                                  all_screens=True)
            full = full.convert("RGB").resize((lw, lh))
        else:
            full = full.crop((0, 0, lw, lh))

        panel = _win_find_panel(hwnd)
        win.panel = panel
        if panel:
            pr = _win_rect(panel)
            box = (round((pr.left - r.left) * s), round((pr.top - r.top) * s),
                   round((pr.right - r.left) * s), round((pr.bottom - r.top) * s))
        else:
            # No device loaded yet: the whole frame. Nothing will be aimed
            # at it, and the load check only needs "is it blank".
            box = (0, 0, lw, lh)
        box = (max(0, box[0]), max(0, box[1]), min(lw, box[2]), min(lh, box[3]))
        if box[2] <= box[0] or box[3] <= box[1]:
            box = (0, 0, lw, lh)
        content = full.crop(box)

        win.retina_scale = s
        win.content_left, win.content_top = box[0], box[1]
        win.content_w, win.content_h = content.width, content.height
        win.geometry_captured = True
        _remember_geometry(win)
        return content


def device_token():
    """An identity for the simulator's current DEVICE INSTANCE, or None.

    Windows: the handle of the device panel, which the simulator destroys and
    re-creates on every push -- an identical re-push of an unchanged build
    included (measured: gone ~1.7 s after monkeydo starts, back with a new
    handle). A token that changed since before a push is therefore proof the
    push was received, the same evidence "the loading screen came and went"
    gives on macOS, and unlike a pixel diff it holds when the new screen is
    identical to the old one. None where there is no such handle (macOS), or
    while no device is up.
    """
    if not IS_WIN:
        return None
    w = _win_find_sim_window()
    return _win_find_panel(w.hwnd) if w else None


_LIST_WINDOWS_SCRIPT = (
    'set FS to (ASCII character 31)\n'
    'tell application "System Events"\n'
    '    set procs to (processes whose name is "simulator")\n'
    '    if (count of procs) is 0 then return ""\n'
    '    set out to ""\n'
    '    repeat with w in (windows of (item 1 of procs))\n'
    '        try\n'
    '            set nm to name of w\n'
    '            if nm is missing value then set nm to ""\n'
    '            set sr to ""\n'
    '            try\n'
    '                set sr to subrole of w\n'
    '            end try\n'
    '            set pos to position of w\n'
    '            set sz to size of w\n'
    '            set bn to ""\n'
    '            try\n'
    '                repeat with b in (buttons of w)\n'
    '                    set bn to bn & (name of b as text) & ","\n'
    '                end repeat\n'
    '            end try\n'
    '            set out to out & nm & FS & sr & FS & (item 1 of pos) & FS & (item 2 of pos)'
    ' & FS & (item 1 of sz) & FS & (item 2 of sz) & FS & bn & linefeed\n'
    '        end try\n'
    '    end repeat\n'
    '    return out\n'
    'end tell'
)


def _mac_sim_windows():
    """Every window of the simulator process, in System Events' order."""
    try:
        r = subprocess.run(["osascript", "-e", _LIST_WINDOWS_SCRIPT],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    return _sw.parse_windows(r.stdout)


#: What dismiss_modal_dialogs / close_aux_windows did since the last drain.
#: A crash or a stray window must never vanish silently: the session attaches
#: these to the step/shot result.
_DISMISSED: list = []


def note_dismissed(kind: str, **detail) -> None:
    ev = {"kind": kind, **detail}
    _DISMISSED.append(ev)
    log.warning("simulator %s: %s", kind, detail)


def drain_dismissed() -> list:
    out = list(_DISMISSED)
    del _DISMISSED[:]
    return out


def close_aux_windows() -> list:
    """Close every simulator window that is not the device window (macOS).

    ``Communications.openWebPage`` opens a non-modal "Webpage Window" over the
    watch; panels and sheets are the same class of thing. They are identified
    by exclusion -- anything the simulator owns that is not the device window or
    an alert dialog -- and closed with their close button, highest index first
    so indices stay valid. Returns the closed windows' names (also noted via
    :func:`note_dismissed`). A no-op off macOS, and with CIQKIT_KEEP_AUX=1.
    """
    if not IS_MAC or os.environ.get("CIQKIT_KEEP_AUX"):
        return []
    aux = _sw.classify(_mac_sim_windows()).aux
    closed = []
    for w in sorted(aux, key=lambda w: -w.index):
        script = (
            'tell application "System Events"\n'
            '    tell (item 1 of (processes whose name is "simulator"))\n'
            f'        set w to window {w.index}\n'
            '        try\n'
            '            click (first button of w whose subrole is "AXCloseButton")\n'
            '            return "ok"\n'
            '        end try\n'
            '        try\n'
            '            perform action "AXCancel" of w\n'
            '            return "ok"\n'
            '        end try\n'
            '        return "no"\n'
            '    end tell\n'
            'end tell')
        try:
            r = subprocess.run(["osascript", "-e", script], capture_output=True,
                               text=True, timeout=4)
            ok = r.stdout.strip() == "ok"
        except Exception:
            ok = False
        note_dismissed("aux_window_closed" if ok else "aux_window_stuck",
                       name=w.name, size=[w.w, w.h])
        if ok:
            closed.append(w.name)
    if closed:
        time.sleep(0.3)
    return closed


_ALERT_PROCS = ("UserNotificationCenter", "CrashReporter", "ReportCrash",
                "Problem Reporter", "CrashReporterSupportHelper")
_SCAN_ALERTS_SCRIPT = (
    'set FS to (ASCII character 31)\n'
    'set out to ""\n'
    'tell application "System Events"\n'
    '    repeat with pn in {@PROCS@}\n'
    '        try\n'
    '            set p to first process whose name is pn\n'
    '            set wi to 0\n'
    '            repeat with w in (windows of p)\n'
    '                set wi to wi + 1\n'
    '                set tx to ""\n'
    '                try\n'
    '                    set tx to tx & (name of w as text) & "|"\n'
    '                end try\n'
    '                try\n'
    '                    repeat with t in (every static text of w)\n'
    '                        set tv to ""\n'
    '                        try\n'
    '                            set tv to (value of t) as text\n'
    '                        end try\n'
    '                        if tv is "" or tv is "missing value" then\n'
    '                            try\n'
    '                                set tv to (name of t) as text\n'
    '                            end try\n'
    '                        end if\n'
    '                        set tx to tx & tv & "|"\n'
    '                    end repeat\n'
    '                end try\n'
    '                set bn to ""\n'
    '                try\n'
    '                    repeat with b in (buttons of w)\n'
    '                        set bn to bn & (name of b as text) & "|"\n'
    '                    end repeat\n'
    '                end try\n'
    '                set out to out & (pn as text) & FS & wi & FS & tx & FS & bn & linefeed\n'
    '            end repeat\n'
    '        end try\n'
    '    end repeat\n'
    '    return out\n'
    'end tell').replace("@PROCS@", ", ".join(f'"{n}"' for n in _ALERT_PROCS))


def _plan_alert_clicks(scan_text: str) -> list:
    """Decide what to click from a scan of alert windows -> [(proc, win, button)].

    Pure (no GUI), so the policy is testable. Safe buttons only: "OK"/"Allow"
    for the ADB notice and the TCC prompt; "OK"/"Ignore"/"Close" for the crash
    reporter's "<app> quit unexpectedly" when the app is simulator-related.
    Never "Report...", "Reopen" (would restart the app behind the broker's
    back), "Don't Allow" or "Deny". A crash is recorded with note_dismissed so
    it is never silent. Highest window index first so a click cannot renumber
    the ones still to do.
    """
    plan = []
    for line in (scan_text or "").splitlines():
        parts = line.split(_sw.FS)
        if len(parts) < 4:
            continue
        pn, wi = parts[0], parts[1]
        texts = parts[2].split("|")
        btns = [b for b in parts[3].split("|") if b]
        try:
            wi = int(wi)
        except ValueError:
            continue
        crashed = _sw.match_crash_dialog(texts)
        if crashed:
            for want in ("OK", "Ignore", "Close"):
                if want in btns:
                    note_dismissed("simulator_crash_dialog", process=crashed,
                                   dialog=" ".join(t for t in texts if t)[:200],
                                   alert_process=pn, clicked=want)
                    plan.append((pn, wi, want))
                    break
            continue
        body = " ".join(texts)
        if pn == "UserNotificationCenter" and "unexpectedly" not in body.lower():
            for want in ("OK", "Allow"):
                if want in btns:
                    plan.append((pn, wi, want))
                    break
    return sorted(plan, key=lambda t: (t[0], -t[1]))


def _dismiss_system_alerts() -> int:
    """Click the safe button of each alert :func:`_plan_alert_clicks` picks."""
    try:
        r = subprocess.run(["osascript", "-e", _SCAN_ALERTS_SCRIPT],
                           capture_output=True, text=True, timeout=4)
    except Exception:
        return 0
    n = 0
    for pn, wi, btn in _plan_alert_clicks(r.stdout):
        script = (
            'tell application "System Events"\n'
            f'    tell (first process whose name is "{pn}")\n'
            f'        click button "{btn}" of window {wi}\n'
            '    end tell\n'
            'end tell')
        try:
            subprocess.run(["osascript", "-e", script], capture_output=True,
                           text=True, timeout=3)
            n += 1
            time.sleep(0.15)
        except Exception:
            pass
    return n


_BLE_MENU = ('tell menu 1 of menu bar item "Settings" of menu bar 1'
             ' to tell menu item "Connection Type" to tell menu 1'
             ' to tell menu item "BLE" to tell menu 1')


def read_phone_connection():
    """The checked item of Settings > Connection Type > BLE ("Connected",
    "Not Connected", "Not Initialized"), or None if unreadable / not macOS."""
    if not IS_MAC:
        return None
    script = (
        'set FS to (ASCII character 31)\n'
        'tell application "System Events" to tell (first process whose name is "simulator")\n'
        f'    {_BLE_MENU}\n'
        '        set out to ""\n'
        '        repeat with mi in menu items\n'
        '            set ch to ""\n'
        '            try\n'
        '                set ch to value of attribute "AXMenuItemMarkChar" of mi\n'
        '            end try\n'
        '            if ch is missing value then set ch to ""\n'
        '            set out to out & (name of mi) & FS & ch & linefeed\n'
        '        end repeat\n'
        '        return out\n'
        '    end tell\n'
        'end tell')
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True,
                           text=True, timeout=6)
    except Exception:
        return None
    return _sw.parse_marked_item(r.stdout)


def set_phone_connection(state) -> dict:
    """Set the simulated phone link (macOS): Settings > Connection Type > BLE >
    Connected / Not Connected, then read the menu back. ``DeviceSettings.
    phoneConnected`` follows it. Returns {ok, requested, state, changed}."""
    want = _sw.phone_menu_item(state)
    if not IS_MAC:
        return {"ok": False, "requested": want, "state": None,
                "error": "phone connection control is implemented for macOS only"}
    before = read_phone_connection()
    if before != want:
        script = (
            'tell application "System Events" to tell (first process whose name is "simulator")\n'
            f'    {_BLE_MENU}\n'
            f'        click menu item "{want}"\n'
            '    end tell\n'
            'end tell')
        try:
            subprocess.run(["osascript", "-e", script], capture_output=True,
                           text=True, timeout=6)
        except Exception:
            pass
        time.sleep(0.5)
    after = read_phone_connection()
    return {"ok": after == want, "requested": want, "state": after,
            "changed": before != after, "previous": before}


def _fit_osascript(script: str, *args, timeout: float = 30) -> dict:
    r = subprocess.run(["osascript", "-e", script, *args], capture_output=True,
                       text=True, timeout=timeout)
    if r.returncode != 0:
        return {"error": (r.stderr or "osascript failed").strip()}
    return _sw.parse_fit_state(r.stdout)


def read_fit_playback() -> dict:
    """The Activity Data window's state ({source, file, elapsed_s, total_s}), or
    {error}. macOS only."""
    if not IS_MAC:
        return {"error": "FIT playback control is implemented for macOS only"}
    return _fit_osascript(_sw.FIT_STATE_SCRIPT, "state")


def start_fit_playback(path: str, advance_s: float = 12.0,
                       close_window: bool = True) -> dict:
    """Simulation > Activity Data > FIT/GPX Playable File: load ``path`` and press
    play, then VERIFY it: the window must show that file, and its playback clock
    must have advanced. ``ok`` is False otherwise, with a ``reason``.

    Returns {ok, file, total_s, elapsed_s, reason?}. macOS only.
    """
    path = os.path.abspath(os.path.expanduser(path))
    res = {"ok": False, "file": path}
    if not IS_MAC:
        res["reason"] = "FIT playback control is implemented for macOS only"
        return res
    if not os.path.isfile(path):
        res["reason"] = f"no such file: {path}"
        return res
    try:
        r = subprocess.run(["osascript", "-e", _sw.FIT_LOAD_SCRIPT, path],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or r.stdout.strip().startswith("ERR"):
            res["reason"] = (r.stdout or r.stderr).strip() or "load script failed"
            return res
        st = _fit_osascript(_sw.FIT_STATE_SCRIPT, "state")
        if st.get("error"):
            res["reason"] = st["error"]
            return res
        if st.get("file") != path:
            res["reason"] = (f"the playback window shows {st.get('file')!r}, not the "
                             f"requested file (the file panel did not take the path)")
            return res
        res["total_s"] = st.get("total_s")
        st = _fit_osascript(_sw.FIT_STATE_SCRIPT, "play")
        deadline = time.monotonic() + advance_s
        while not st.get("error") and not (st.get("elapsed_s") or 0) > 0:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.7)
            st = _fit_osascript(_sw.FIT_STATE_SCRIPT, "state")
        if st.get("error"):
            res["reason"] = st["error"]
            return res
        res["elapsed_s"] = st.get("elapsed_s")
        if not (st.get("elapsed_s") or 0) > 0:
            res["reason"] = (f"playback clock did not advance within {advance_s:.0f}s "
                             f"(stuck at {st.get('elapsed_s')})")
            return res
        res["ok"] = True
        if close_window:
            # the panel sits over the watch face; playback goes on without it
            _fit_osascript(_sw.FIT_STATE_SCRIPT, "close")
    except subprocess.TimeoutExpired:
        res["reason"] = "System Events did not answer while driving the Activity Data window"
    return res


def find_sim_window():
    """Locate the CIQ Simulator window.  Returns platform-specific handle."""
    if IS_WIN:
        return _win_find_sim_window()

    elif IS_MAC:
        dev = _sw.classify(_mac_sim_windows()).device
        if dev is None:
            return None
        w = _MacWindow(dev.x, dev.y, dev.w, dev.h)
        # Callers rebuild this handle before every gesture; without this the
        # last capture's geometry would be discarded each time and every tap
        # would ignore the chrome offset. Only re-attached if the window has
        # not moved -- see _restore_geometry.
        _restore_geometry(w)
        return w
    return None


def find_canvas(main_hwnd):
    """Locate the interaction canvas inside the simulator window.

    Windows: the same handle, with ``panel`` (the device's wxWindowNR child)
             resolved afresh -- it is re-created by every push. Gestures are
             aimed through the handle's capture geometry, so the handle, not
             the bare child, is what callers need.
    macOS:   the whole window is the canvas -- returns main_hwnd unchanged.
    """
    if IS_WIN:
        if main_hwnd is None:
            return None
        if isinstance(main_hwnd, _WinWindow):
            main_hwnd.panel = _win_find_panel(main_hwnd.hwnd)
            return main_hwnd
        return _win_find_panel(main_hwnd)
    else:
        return main_hwnd


def restore_window(hwnd) -> None:
    """Cross-platform SW_RESTORE: un-minimise and bring the window into view."""
    if IS_WIN:
        # Only when minimised: SW_RESTORE on a MAXIMISED window un-maximises
        # it, resizing it under the geometry the last capture measured.
        if user32.IsIconic(_hwnd(hwnd)):
            user32.ShowWindow(_hwnd(hwnd), 9)   # SW_RESTORE
    elif IS_MAC:
        script = (
            'tell application "System Events"\n'
            '    set procs to (processes whose name is "simulator")\n'
            '    if (count of procs) > 0 then\n'
            '        set frontmost of (item 1 of procs) to true\n'
            '    end if\n'
            'end tell'
        )
        subprocess.run(["osascript", "-e", script], capture_output=True)


def dismiss_modal_dialogs(verbose=False):
    """Close any stray modal dialogs occluding the watch face.

    Windows: posts WM_CLOSE to #32770 dialogs with error/warning titles.
    macOS:   clicks "OK" on ConnectIQ simulator error dialogs (e.g. ADB/data
             connection prompt) so automated flows are not blocked.
    """
    if IS_MAC:
        script = (
            'tell application "System Events"\n'
            '    set closedCount to 0\n'
            '    set procs to (processes whose name is "simulator")\n'
            '    if (count of procs) is 0 then return "0"\n'
            '    set p to item 1 of procs\n'
            '    repeat with w in (windows of p)\n'
            '        set shouldClose to false\n'
            '        try\n'
            '            if (name of w) contains "Error" then set shouldClose to true\n'
            '        end try\n'
            '        if not shouldClose then\n'
            '            try\n'
            '                set texts to value of every static text of w\n'
            '                repeat with t in texts\n'
            '                    set s to (t as text)\n'
            '                    if s contains "There is no data connection" then set shouldClose to true\n'
            '                    if s contains "connect an Android device to ADB" then set shouldClose to true\n'
            '                end repeat\n'
            '            end try\n'
            '        end if\n'
            '        if shouldClose then\n'
            '            try\n'
            '                click button "OK" of w\n'
            '                set closedCount to closedCount + 1\n'
            '            end try\n'
            '        end if\n'
            '    end repeat\n'
            '    return closedCount as text\n'
            'end tell'
        )
        try:
            res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=2)
            closed = int((res.stdout or "0").strip() or "0")
        except Exception:
            closed = 0

        # On SDK 9.1.0 / recent macOS the simulator's ADB "no data connection /
        # run adb forward tcp:7381" alert and the "'node' would like to access
        # data from other apps" TCC prompt are NOT child windows of the
        # `simulator` process — they are presented by **UserNotificationCenter**.
        # The loop above never sees them, so they sit APP-MODAL over the watch
        # canvas and silently swallow every pm_click tap (a menu-gated screen
        # then looks "unresponsive to taps"). Click their safe buttons here.
        # "Allow" grants node file access once and stops the TCC prompt
        # recurring; "OK" acknowledges the ADB info alert. We never touch
        # "Don't Allow"/"Deny" (would revoke a grant) or crash-report buttons.
        closed += _dismiss_system_alerts()
        closed += len(close_aux_windows())

        if closed and verbose:
            log.debug(f"  Dismissed {closed} simulator dialog(s)")
            time.sleep(0.2)
        return closed

    if not IS_WIN:
        return 0

    WM_CLOSE = 0x0010
    closed = 0
    # Only the SIMULATOR's dialogs. This used to close every visible message
    # box on the desktop whose title said "error", "warning", "information"
    # or "confirm" -- including the user's own editor asking whether to save.
    sim_pids = _win_sim_pids()
    if not sim_pids:
        return 0
    triggers = ("there is no data connection", "adb")

    def _cb(hwnd, _):
        nonlocal closed
        if not user32.IsWindowVisible(hwnd) or _win_pid(hwnd) not in sim_pids:
            return True
        if _win_class(hwnd) != "#32770":
            return True
        title = _win_text(hwnd)
        title_lc = title.lower()
        hit = any(t in title_lc for t in ("error", "warning", "information", "confirm"))
        if not hit:
            body = " ".join(_win_text(c) for c in _win_children(hwnd)).lower()
            hit = any(t in body for t in triggers)
        if hit:
            if verbose:
                log.debug(f"  Dismissing dialog: {title!r}")
            # WM_CLOSE on a message box is its Cancel/OK -- never a "Yes".
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            closed += 1
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    if closed:
        time.sleep(0.3)
    return closed


def get_window_rect(hwnd):
    """Return window bounds in screen coordinates as a RECT-like object.

    Windows: ctypes RECT in physical pixels.
    macOS:   _Rect in logical pixels, already offset by content_left/top, so
             the values map straight onto pyautogui's coordinate space.
    """
    if IS_WIN:
        return _win_rect(_hwnd(hwnd))
    elif IS_MAC:
        scale = getattr(hwnd, 'retina_scale', 1.0)
        left = hwnd.left + int(hwnd.content_left / scale)
        t = hwnd.top  + int(hwnd.content_top  / scale)
        r = hwnd.left + hwnd.width
        b = hwnd.top  + hwnd.height
        return _Rect(left, t, r, b)
    return _Rect(0, 0, 0, 0)


def focus_window(hwnd) -> None:
    """Bring the simulator window to the foreground.

    Windows: foreground + keyboard focus on the device panel (see _win_focus).
    macOS:   uses osascript to set frontmost process and raise the DEVICE window
             above any auxiliary window (a web view must never be the capture).
    """
    if IS_WIN:
        if not isinstance(hwnd, _WinWindow):
            hwnd = find_sim_window() or hwnd
        if isinstance(hwnd, _WinWindow):
            _win_focus(hwnd)
    elif IS_MAC:
        script = (
            'tell application "System Events"\n'
            '    set procs to (processes whose name is "simulator")\n'
            '    if (count of procs) > 0 then\n'
            '        set frontmost of (item 1 of procs) to true\n'
            '        try\n'
            '            perform action "AXRaise" of (first window of (item 1 of procs)'
            f' whose name starts with "{_sw.DEVICE_TITLE_PREFIX}")\n'
            '        end try\n'
            '    end if\n'
            'end tell'
        )
        subprocess.run(["osascript", "-e", script], capture_output=True)
        time.sleep(0.3)
# Screenshot capture
# ---------------------------------------------------------------------------


#: macOS settle between raising the window and reading its pixels. One shot can
#: afford to be certain the window finished coming forward; a sampling loop that
#: already raised it once cannot -- see `grab_settle_s` below.
DEFAULT_GRAB_SETTLE_S = 1.0


def _quartz_region_png(x: int, y: int, w: int, h: int, path: str) -> bool:
    """Grab the on-screen rect (logical px) to a PNG via Quartz. False on any
    failure, so the caller falls back to the `screencapture` CLI."""
    try:
        import Quartz
        from Foundation import NSURL
        img = Quartz.CGWindowListCreateImage(
            Quartz.CGRectMake(x, y, w, h), Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID, Quartz.kCGWindowImageDefault)
        if img is None or Quartz.CGImageGetWidth(img) == 0:
            return False
        dest = Quartz.CGImageDestinationCreateWithURL(
            NSURL.fileURLWithPath_(path), "public.png", 1, None)
        Quartz.CGImageDestinationAddImage(dest, img, None)
        return bool(Quartz.CGImageDestinationFinalize(dest))
    except Exception:
        return False


def capture_window(hwnd, *, grab_settle_s: float | None = None,
                   refocus: bool = True):
    """Capture the simulator window.  Returns (PIL Image, dpi_scale).

    Windows: PrintWindow of the frame, cropped to the device panel, at the
             simulator's own (DPI-unaware, i.e. native watch) pixel size. It
             reads the window's drawing rather than the screen, so it needs
             neither focus nor a settle and is correct when partly covered;
             `refocus` only un-minimises it. ~0.07 s.
    macOS:   `screencapture` over the window region; image is already
             in logical pixels, so dpi_scale is always 1.0.

    On macOS the grab itself -- `screencapture` plus the chrome trim -- is
    ~0.15 s. Everything else in the default path exists to *guarantee* the
    window is where we think it is and on top: an AppleScript position probe
    (~0.13 s), a restore (~0.15 s), a focus (~0.57 s, 0.3 s of it its own
    sleep) and then `grab_settle_s` on top. Measured total ~2.1 s. That is the
    right trade for a one-shot screenshot, and it is the whole reason recorded
    clips used to sample at ~0.5 fps.

    `refocus=False` skips the probe, the restore and the focus (~0.85 s);
    `grab_settle_s=0` skips the settle. A caller that has already raised the
    window and knows nothing else drives the simulator (the broker is the
    single sim actor) gets the bare ~0.15 s grab that way. Both are macOS-only
    knobs; the Windows path has no such cost and ignores them.
    """
    if IS_WIN:
        if not isinstance(hwnd, _WinWindow):
            hwnd = find_sim_window()
            if hwnd is None:
                return Image.new("RGB", (1, 1), (0, 0, 0)), 1.0
        if refocus and user32.IsIconic(hwnd.hwnd):
            user32.ShowWindow(hwnd.hwnd, 9)   # SW_RESTORE
            time.sleep(0.3)
        return _win_capture(hwnd), 1.0

    elif IS_MAC:
        if refocus:
            # Refresh window position in case it moved
            updated = find_sim_window()
            if updated is not None:
                hwnd.left, hwnd.top = updated.left, updated.top
                hwnd.width, hwnd.height = updated.width, updated.height

            restore_window(hwnd)
            focus_window(hwnd)

        settle = DEFAULT_GRAB_SETTLE_S if grab_settle_s is None else grab_settle_s
        if settle > 0:
            time.sleep(settle)

        region = (hwnd.left, hwnd.top, hwnd.width, hwnd.height)
        x, y, w, h = [int(v) for v in region]

        # pyautogui/pyscreeze is unreliable on newer macOS setups and can leave
        # behind unreadable temp files. Native screencapture is more stable.
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            # Quartz first: the `screencapture` CLI can hang machine-wide
            # (2026-09-28: every call timed out while CGWindowListCreateImage
            # answered in ~16 ms), which failed every load verification with
            # "pre-push baseline frame could not be captured". Same pixels:
            # an on-screen region grab at native (Retina) resolution.
            if not _quartz_region_png(x, y, w, h, tmp_path):
                proc = subprocess.run(
                    ["screencapture", "-x", "-R", f"{x},{y},{w},{h}", tmp_path],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if proc.returncode != 0:
                    raise RuntimeError((proc.stderr or proc.stdout or "screencapture failed").strip())

            with Image.open(tmp_path) as captured:
                img = captured.convert("RGB")
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        # Trim OS chrome / simulator title bar
        arr     = np.array(img).astype(np.float32)
        gray    = np.mean(arr[:, :, :3], axis=2)
        col_max = np.max(gray, axis=0)
        row_max = np.max(gray, axis=1)
        content_cols = np.where(col_max > 5)[0]
        content_rows = np.where(row_max > 5)[0]

        # screencapture -R uses logical pixels but outputs native (Retina) resolution.
        # Compute the physical-to-logical scale so pm_click/pm_swipe can convert
        # physical pixel coords back to logical coords for pyautogui.
        retina_scale = img.width / hwnd.width if hwnd.width > 0 else 1.0
        hwnd.retina_scale = retina_scale

        if len(content_cols) > 0 and len(content_rows) > 0:
            cl, cr = int(content_cols[0]), int(content_cols[-1]) + 1
            ct, cb = int(content_rows[0]), int(content_rows[-1]) + 1
            content_img = img.crop((cl, ct, cr, cb))
            hwnd.content_left = cl
            hwnd.content_top  = ct
        else:
            content_img = img
            hwnd.content_left = 0
            hwnd.content_top  = 0
        hwnd.content_w = content_img.width
        hwnd.content_h = content_img.height
        # This geometry describes THIS capture of the window at its current
        # position. It goes stale the moment the window moves, which is why the
        # cache below is keyed on the rect.
        hwnd.geometry_captured = True
        _remember_geometry(hwnd)

        return content_img, 1.0

    return Image.new("RGB", (1, 1), (0, 0, 0)), 1.0


# ---------------------------------------------------------------------------
# Simulator interaction
# ---------------------------------------------------------------------------

# ── capture geometry, and why it has to be remembered ─────────────────────────
# The mapping from "a point on the watch display" to "a point on the screen"
# is only knowable from a capture: it needs the offset of the simulator's
# display content inside its window (the chrome the capture trims) and the
# Retina scale of the grab. capture_window() computes both.
#
# But callers rebuild their window handle constantly -- SimSession._rebind()
# does it immediately before every gesture -- and a handle fresh from
# find_sim_window() carries only the neutral defaults. The geometry was
# therefore computed, stored on a handle, and thrown away again before it was
# ever used, so every tap silently ignored the chrome offset.
#
# Caching it against the window RECT fixes that without pretending to know more
# than we do: if the window has not moved or resized, the last capture's
# geometry still describes it; if it has, we are honestly back to knowing
# nothing and say so.
_LAST_GEOMETRY = None   # (rect, content_left, content_top, content_w, content_h,
#                          scale, display_box)


def _remember_geometry(hwnd) -> None:
    global _LAST_GEOMETRY
    _LAST_GEOMETRY = (hwnd.rect(), hwnd.content_left, hwnd.content_top,
                      hwnd.content_w, hwnd.content_h, hwnd.retina_scale,
                      getattr(hwnd, "display_box", None))


def _restore_geometry(hwnd) -> None:
    """Re-attach the last capture's geometry if it still describes this window."""
    if _LAST_GEOMETRY is None:
        return
    rect, cl, ct, cw, ch, scale, box = _LAST_GEOMETRY
    if rect != hwnd.rect():
        return                      # moved or resized -- genuinely stale
    hwnd.content_left, hwnd.content_top = cl, ct
    hwnd.content_w, hwnd.content_h = cw, ch
    hwnd.retina_scale = scale
    hwnd.display_box = box
    hwnd.geometry_captured = True


#: Whether gesture fractions are fractions of the watch DISPLAY (when a
#: session has located it) rather than of the whole content area. The content
#: is the simulator's device panel, which draws the watch body and band around
#: the screen on Windows AND on macOS (measured on fr265s, SDK 9.1.0: the
#: window is the whole watch; y=0.42 of it is the title above the screen's
#: first button, not 42% down the screen), so y=0.85 of the panel is the bezel,
#: not the bottom of the screen the docs promise. A face that cannot be found
#: leaves the old whole-content behaviour.
FRACTIONS_OF_DISPLAY = IS_WIN or IS_MAC


def set_display_box(hwnd, box) -> None:
    """Record where the watch display sits inside the last capture's content
    image (x0, y0, x1, y1), so gesture fractions aim at the screen. Kept with
    the capture geometry: it goes stale exactly when that does."""
    if hwnd is None or not getattr(hwnd, "geometry_captured", False):
        return
    hwnd.display_box = tuple(int(v) for v in box) if box else None
    _remember_geometry(hwnd)


def content_point(canvas, fx: float, fy: float):
    """Logical screen coordinates of the point at fraction (fx, fy) of the
    simulator's DISPLAY CONTENT.

    Fractions, not pixels, because the two ends disagreed about pixels and it
    was never visible in a signature: ``content_left``/``content_top`` are in
    captured-image (physical) pixels, while callers passed coordinates derived
    from the window's LOGICAL size. The old code added the two and divided the
    sum by the Retina scale -- dimensionally wrong, and only harmless because
    the offsets were always zero by the time a click read them.

    Here the whole computation stays in image pixels and converts once, at the
    end. With no chrome and no Retina scaling this is identical to the old
    window-fraction behaviour; the difference is exactly the offset that was
    being dropped.
    """
    scale = getattr(canvas, "retina_scale", 1.0) or 1.0
    if (getattr(canvas, "geometry_captured", False)
            and getattr(canvas, "content_w", 0) and getattr(canvas, "content_h", 0)):
        box = getattr(canvas, "display_box", None) if FRACTIONS_OF_DISPLAY else None
        if box:
            x0, y0, x1, y1 = box
            ix = canvas.content_left + x0 + (x1 - x0) * fx    # image px
            iy = canvas.content_top + y0 + (y1 - y0) * fy
        else:
            ix = canvas.content_left + canvas.content_w * fx
            iy = canvas.content_top + canvas.content_h * fy
        return canvas.left + int(ix / scale), canvas.top + int(iy / scale)
    # Never captured: fall back to fractions of the whole window, which is what
    # this did before the content geometry existed. It aims at the window
    # rather than the display, so say so.
    log.warning(
        "aiming at a window handle that has never been captured: falling back "
        "to whole-window fractions, which include the simulator's chrome. "
        "Call capture_window() on this handle first.")
    return (canvas.left + int(canvas.width * fx),
            canvas.top + int(canvas.height * fy))


def content_span(canvas, frac: float) -> int:
    """Logical-pixel length of `frac` of the display content's width."""
    scale = getattr(canvas, "retina_scale", 1.0) or 1.0
    if getattr(canvas, "geometry_captured", False) and getattr(canvas, "content_w", 0):
        box = getattr(canvas, "display_box", None) if FRACTIONS_OF_DISPLAY else None
        width = (box[2] - box[0]) if box else canvas.content_w
        return int(width * frac / scale)
    return int(canvas.width * frac)


def _screen_xy(canvas, cx, cy):
    """Map content-relative (cx, cy) to logical screen coordinates.

    The offsets come from the last :func:`capture_window` on THIS handle. A
    handle straight from :func:`find_sim_window` has never been captured, so
    its offsets are still the neutral zeros -- clicks then ignore whatever OS
    chrome the capture would have trimmed, and land shifted by the height of
    the simulator's title bar. Say so rather than aiming blind: an input that
    misses and is not noticed is how a "verified" gesture ends up verifying
    the wrong screen.
    """
    if not getattr(canvas, "geometry_captured", False):
        log.warning(
            "clicking on a window handle that has never been captured: its "
            "content offset and retina scale are still at their defaults, so "
            "the target may be off by the simulator's chrome. Call "
            "capture_window() on this handle first.")
    scale = getattr(canvas, "retina_scale", 1.0) or 1.0
    sx = canvas.left + int((getattr(canvas, "content_left", 0) + cx) / scale)
    sy = canvas.top  + int((getattr(canvas, "content_top", 0) + cy) / scale)
    return sx, sy


def _prepare_input(canvas) -> None:
    """Make sure the point a gesture is about to be computed for is on-screen.

    Windows only: real input lands only where the window is visible, and the
    simulator happily restores itself partly below the taskbar. Must run
    BEFORE the target is computed, since it may move the window.
    """
    if IS_WIN and isinstance(canvas, _WinWindow):
        _win_bring_into_view(canvas)


def pm_click_frac(canvas, fx: float, fy: float, delay=2.0):
    """Tap at fraction (fx, fy) of the simulator's display content.

    The fraction-based entry point exists because the pixel one had no single
    coordinate system: see :func:`content_point`. Callers think in "80% down
    the watch face", so let them say that and do the conversion in one place.

    Real input on both platforms. On Windows that is measured, not a choice:
    the simulator ignores PostMessage'd clicks entirely.
    """
    _prepare_input(canvas)
    sx, sy = content_point(canvas, fx, fy)
    focus_window(canvas)
    pyautogui.click(sx, sy)
    time.sleep(delay)


def pm_hold_frac(canvas, fx: float, fy: float, hold_s: float = 1.6, delay=1.5):
    """Touch-and-hold at fraction (fx, fy) of the display: the simulator
    delivers it as a long press (BehaviorDelegate.onHold, and onMenu on
    touch-primary devices)."""
    if not (IS_MAC or IS_WIN):
        return pm_click_frac(canvas, fx, fy, delay=delay)
    _prepare_input(canvas)
    sx, sy = content_point(canvas, fx, fy)
    focus_window(canvas)
    pyautogui.moveTo(sx, sy, duration=0.05)
    pyautogui.mouseDown()
    time.sleep(hold_s)
    pyautogui.mouseUp()
    time.sleep(delay)


def pm_press_point(canvas, cx, cy, hold_s: float = 0.0, delay=1.5):
    """Press at the captured-content pixel (cx, cy), optionally held for
    ``hold_s`` seconds. Used for the device art's physical buttons, which sit
    outside the display and so cannot be addressed by display fractions."""
    if not (IS_MAC or IS_WIN):
        raise RuntimeError("button presses need macOS or Windows")
    _prepare_input(canvas)
    sx, sy = _screen_xy(canvas, cx, cy)
    focus_window(canvas)
    if hold_s > 0:
        pyautogui.moveTo(sx, sy, duration=0.05)
        pyautogui.mouseDown()
        time.sleep(hold_s)
        pyautogui.mouseUp()
    else:
        pyautogui.click(sx, sy)
    time.sleep(delay)


def pm_swipe_frac(canvas, direction="up", span=0.84, delay=1.5):
    """Flick across the display content, `span` being a fraction of its width."""
    _prepare_input(canvas)
    cx, cy = content_point(canvas, 0.5, 0.5)
    return _mac_drag(canvas, cx, cy, content_span(canvas, span),
                     direction=direction, delay=delay)


def pm_click(canvas, cx, cy, delay=2.0):
    """Click the simulator canvas -- triggers onSelect / ACTION button.

    (cx, cy) are pixels of the captured content image; prefer
    :func:`pm_click_frac`. Real input at the mapped screen position on both
    platforms.
    """
    if IS_MAC or IS_WIN:
        _prepare_input(canvas)
        sx, sy = _screen_xy(canvas, cx, cy)
        focus_window(canvas)
        pyautogui.click(sx, sy)
    time.sleep(delay)


def pm_swipe(canvas, cx, cy, radius, direction="up", delay=1.5):
    """Drag on the canvas -- triggers onNextPage / onPreviousPage / onSwipe.

    Supports vertical (up/down) and horizontal (left/right). Horizontal
    drags are how InputDelegate.onSwipe(SWIPE_LEFT|RIGHT) is reached
    in tabbed views (BSA History).
    """
    if IS_MAC or IS_WIN:
        _prepare_input(canvas)
        mx, my = _screen_xy(canvas, cx, cy)
        scale = getattr(canvas, "retina_scale", 1.0) or 1.0
        _mac_drag(canvas, mx, my, radius / scale, direction=direction, delay=0)
    time.sleep(delay)


def _mac_drag(canvas, mx, my, span, direction="up", delay=1.5):
    """Drag through `span` LOGICAL pixels, centred on the logical point (mx, my).

    Centred on purpose: half the span before the anchor, half after. Keeps BOTH
    ends mid-face -- a one-sided drag toward the right/bottom lands in the bezel
    / edge back-swipe zone and the sim drops it (right-swipe was silently
    ignored while left worked).

    `span` is already in logical pixels; do not re-divide it by the Retina
    scale here. Mixing the two coordinate systems is what made the old click
    path wrong, so this one takes a single documented unit.
    """
    h = int(span * 0.25)                 # half-span; full drag ~= span*0.5
    ux, uy = _DIRS.get(direction, (None, None))
    if ux is None:
        raise ValueError(f"unknown direction: {direction!r}")
    sx, sy = mx - ux * h, my - uy * h
    ex, ey = mx + ux * h, my + uy * h
    _drag(canvas, sx, sy, ex, ey, delay)


_DIRS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}


def drag_ends(sx, sy, direction, length):
    """End point of a drag of ``length`` from (sx, sy) toward ``direction``."""
    ux, uy = _DIRS.get(direction, (None, None))
    if ux is None:
        raise ValueError(f"unknown direction: {direction!r}")
    return sx + ux * length, sy + uy * length


def pm_swipe_from_frac(canvas, fx, fy, direction="left", dist=0.4, delay=1.5):
    """Swipe that STARTS at display fraction (fx, fy) and runs ``dist`` (a
    fraction of the display width) toward ``direction``.

    Unlike :func:`pm_swipe_frac` (centred on the display, so both ends stay
    mid-face), this starts where it is told -- which is what an edge swipe is:
    ``(0.97, 0.5)`` + ``left`` pulls in from the right edge.
    """
    _prepare_input(canvas)
    sx, sy = content_point(canvas, fx, fy)
    ex, ey = drag_ends(sx, sy, direction, content_span(canvas, dist))
    _drag(canvas, sx, sy, ex, ey, delay)


def _drag(canvas, sx, sy, ex, ey, delay):
    focus_window(canvas)
    pyautogui.moveTo(sx, sy, duration=0.05)
    pyautogui.mouseDown()
    time.sleep(0.12)  # let the sim register the press before the drag begins
    # Step the drag through several discrete moves so the sim sees a real
    # gesture (a single moveTo can land as a click on some builds).
    steps = 12
    for i in range(1, steps + 1):
        t = i / float(steps)
        pyautogui.moveTo(int(sx + (ex - sx) * t), int(sy + (ey - sy) * t),
                         duration=0.03)
    time.sleep(0.05)
    pyautogui.mouseUp()
    time.sleep(delay)


def phys_key(hwnd, key, delay=1.5, modifiers=None):
    """Send a keystroke to the simulator via the OS's native input path.

    On macOS uses AppleScript `System Events` keystroke / key code, which
    lands every event reliably (unlike pyautogui's keystroke path, which
    drops the first event after a focus change on CIQ SDK 9.1.0).

    `key`        — pyautogui-style name. Arrows ("up","down","left","right"),
                    "return"/"enter", "escape"/"esc", "tab", "space",
                    "delete", or any printable char.
    `modifiers`  — list of "command","shift","option","control".
    """
    if IS_MAC:
        keycode_map = {
            "up": 126, "down": 125, "left": 123, "right": 124,
            "return": 36, "enter": 36,
            "escape": 53, "esc": 53,
            "space": 49, "tab": 48, "delete": 51,
        }
        mod_map = {
            "command": "command down", "cmd":     "command down",
            "shift":   "shift down",
            "option":  "option down",  "alt":     "option down",
            "control": "control down", "ctrl":    "control down",
        }
        using_clause = ""
        if modifiers:
            phrases = [mod_map[m] for m in modifiers if m in mod_map]
            if phrases:
                using_clause = " using {" + ", ".join(phrases) + "}"
        if key in keycode_map:
            action = f"key code {keycode_map[key]}{using_clause}"
        else:
            action = f'keystroke "{key}"{using_clause}'
        script = (
            'tell application "System Events"\n'
            '    set procs to (processes whose name is "simulator")\n'
            '    if (count of procs) > 0 then\n'
            '        set frontmost of (item 1 of procs) to true\n'
            '        delay 0.15\n'
            f'        {action}\n'
            '    end if\n'
            'end tell'
        )
        subprocess.run(["osascript", "-e", script], capture_output=True)
        time.sleep(delay)
        return

    # Windows: real key events (the simulator ignores posted WM_KEYDOWN),
    # after focus_window has given the device panel keyboard focus -- with
    # the frame foreground but the panel unfocused, keys are dropped. The
    # simulator maps Enter to START and Esc to BACK; arrows only do anything
    # on devices that have UP/DOWN buttons.
    focus_window(hwnd)
    time.sleep(0.2)
    if modifiers:
        for m in modifiers:
            pyautogui.keyDown(m)
    pyautogui.press(key)
    if modifiers:
        for m in reversed(modifiers):
            pyautogui.keyUp(m)
    time.sleep(delay)


# ---------------------------------------------------------------------------
# Kill orphan monkeydo Java processes
# ---------------------------------------------------------------------------

def _kill_orphan_monkeydo():
    """Kill any leftover monkeydo wrapper + Java child that block the push socket.

    On macOS the wrapper is a shell launcher; the actual push is performed by a
    Java process whose command line contains ``MonkeyDoDeux``. A previous
    push that timed out can leave the Java child holding the simulator's push
    socket, which silently rejects later pushes. Kill both.

    Scoped to our own uid (``pkill -u``): the pattern is a command-line
    substring, and on a shared machine another user's process could match it.
    Ours are the only ones we have any business killing.

    Windows: the same match, done in-process -- java.exe processes in our own
    logon session whose command line names MonkeyDoDeux. (This used to shell
    out to ``wmic``, which current Windows 11 no longer ships, so it silently
    did nothing.) With their children: TerminateProcess takes none along, and
    every killed push left its shell.exe running and connected to the
    simulator -- one more per push. Shells already orphaned that way are
    swept too.
    """
    if IS_WIN:
        try:
            _kill_monkeydo_trees_win()
        except Exception:
            pass   # best effort: a stale push socket costs a retry, not a capture
    elif IS_MAC:
        for pattern in ("monkeydodeux", "MonkeyDoDeux"):
            subprocess.run(["pkill", "-u", str(os.getuid()), "-f", pattern],
                           capture_output=True)


# What a monkeydo java process starts: the SDK's shell.exe, or simport's shim
# (cmd.exe running shellrelay.py on python.exe). Only these names are followed
# down the tree, so a stale parent pid that happens to match cannot take an
# unrelated program with it.
_MONKEYDO_CHILDREN = ("shell.exe", "cmd.exe", "python.exe")


def _kill_monkeydo_trees_win():
    from .. import _winapi
    mine = _winapi.session_id()
    procs = _winapi.processes()
    alive = {pid for pid, _pp, _n in procs}
    kids: dict = {}
    for pid, ppid, name in procs:
        kids.setdefault(ppid, []).append((pid, name.lower()))
    doomed = []
    for pid, ppid, name in procs:
        name = name.lower()
        if name in ("java.exe", "javaw.exe"):
            if (_winapi.session_id(pid) == mine
                    and "monkeydodeux" in (_winapi.command_line(pid) or "").lower()):
                doomed.append(pid)
                stack = [pid]
                while stack:
                    for child, cname in kids.get(stack.pop(), ()):
                        if cname in _MONKEYDO_CHILDREN:
                            doomed.append(child)
                            stack.append(child)
        elif name == "shell.exe" and ppid not in alive:
            if (_winapi.session_id(pid) == mine
                    and "--transport_args=127.0.0.1:" in (_winapi.command_line(pid) or "")):
                doomed.append(pid)
    for pid in doomed:
        _winapi.terminate(pid)


# ---------------------------------------------------------------------------
# Simulator lifecycle
# ---------------------------------------------------------------------------

def ensure_simulator():
    """Start the CIQ Simulator if not already running.  Returns hwnd.

    When the simulator is already up, returns immediately. When it is not,
    polls every 250 ms (instead of 1 s) for up to 30 s, then waits briefly
    for the window to fully render before returning so the next monkeydo
    push lands on a ready window.
    """
    hwnd = find_sim_window()
    if hwnd:
        return hwnd
    if not _sdk:
        raise SimulatorLaunchError(
            "Connect IQ SDK not found: screenshot_utils.init(sdk, project) has "
            "not been called, or CIQ_HOME points nowhere. Run `ciqkit doctor`.")
    log.debug("Starting simulator...")
    if IS_MAC:
        # macOS: the simulator binary is a wxWidgets/Carbon GUI app and MUST
        # be launched via LaunchServices (`open -a`) so it is registered as a
        # foreground app. Launching the binary directly via subprocess.Popen
        # makes macOS classify it as a background process and the app calls
        # abort() during startup (SIGABRT, procRole=Background in the crash
        # report). It also requires a real local console session — running
        # over Jump Desktop / VNC / Screen Sharing typically still fails with
        # the same abort, because wxWidgets cannot attach to the WindowServer.
        app_bundle = os.path.join(_sdk, "bin", "ConnectIQ.app")
        if not os.path.isdir(app_bundle):
            raise SimulatorLaunchError(
                f"simulator app bundle not found: {app_bundle}")
        subprocess.Popen(["open", "-a", app_bundle],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        sim_candidates = []
        if IS_WIN:
            sim_candidates.append(os.path.join(_sdk, "bin", "simulator.exe"))
        else:
            sim_candidates.append(os.path.join(_sdk, "bin", "simulator"))
        sim_exe = next((p for p in sim_candidates if os.path.exists(p)), None)
        if not sim_exe:
            raise SimulatorLaunchError(
                f"simulator not found in any expected path: {sim_candidates}")
        # Detached (and on Windows console-less and out of the caller's job
        # object): the simulator must outlive the broker that started it, or
        # every idle-exit would take the user's simulator down with it.
        from .._compat import detached_popen
        detached_popen([sim_exe], gui=True, cwd=_project or ".",
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       stdin=subprocess.DEVNULL)
    deadline = time.time() + 30.0
    while time.time() < deadline:
        time.sleep(0.25)
        hwnd = find_sim_window()
        if hwnd:
            # Confirm the window is fully realized: poll until a capture
            # succeeds OR a short bounded settle elapses. Faster than the
            # blanket 2 s sleep on machines where the window paints quickly --
            # but only with the preamble off. find_sim_window() just returned
            # this handle, so re-probing it is redundant, and a default capture
            # costs ~2.1 s, which fits into a 2.5 s budget at most once: the
            # loop was slower than the sleep it replaced. At ~0.15 s a look it
            # polls ~15 times and returns as soon as the window paints.
            settle_deadline = time.time() + 2.5
            while time.time() < settle_deadline:
                try:
                    img, _ = capture_window(hwnd, refocus=False,
                                            grab_settle_s=0)
                    if img is not None and img.size[0] > 50:
                        return hwnd
                except Exception:
                    pass
                time.sleep(0.2)
            return hwnd
    if IS_MAC:
        raise SimulatorLaunchError(
            "simulator did not start within 30 seconds.\n"
            "  On macOS it needs a real local graphical session; a remote one "
            "(VNC, Screen Sharing, SSH) can abort it.\n"
            "  Check ~/Library/Logs/DiagnosticReports/simulator-*.ips")
    if IS_WIN:
        raise SimulatorLaunchError(
            "simulator did not start within 30 seconds.\n"
            "  It needs an interactive desktop: not a service, a scheduled "
            "task without 'run only when user is logged on', or a locked "
            "session.\n"
            "  Try starting bin\\simulator.exe from the SDK by hand to see "
            "its own error.")
    raise SimulatorLaunchError("simulator did not start within 30 seconds")


def _restart_simulator():
    """Kill and restart the CIQ simulator.  Returns new hwnd.

    This is a hard kill of the running simulator, so anything the loaded app
    held in memory is gone. ciqkit escalates to it only when a push has failed
    to produce a loaded app; if you had the simulator open for your own
    debugging, that session goes with it.

    ``pkill -x`` matches the process name exactly (``simulator``, not Apple's
    ``Simulator``) and ``-u`` limits it to our own uid, so it cannot reach a
    process belonging to anyone else on the machine. On Windows the same
    scoping is our own logon session -- ``taskkill /im simulator.exe``, what
    this used to run, kills every user's simulator when run elevated.
    """
    hwnd = find_sim_window()
    if hwnd:
        log.info("restarting the Connect IQ simulator")
        if IS_WIN:
            from .. import _winapi
            for pid in _winapi.pids_named("simulator.exe"):
                _winapi.terminate(pid)
        elif IS_MAC:
            subprocess.run(["pkill", "-x", "-u", str(os.getuid()), "simulator"],
                           capture_output=True)
        time.sleep(2)
    return ensure_simulator()




