"""Which macOS windows belong to the simulator, and what each one is.

Pure parsing and classification so it runs in CI without a simulator: the
AppleScript that *reads* the windows lives in ``screenshot_utils``; this module
turns its text into decisions.

The simulator process (``simulator``) owns more than the watch. Measured on SDK
9.1.0 / macOS 26: the device window is titled ``CIQ Simulator - <Device> (<ver>)``
and everything else is auxiliary -- the ``Communications.openWebPage`` "Webpage
Window", the data-field/sensor panels, alert sheets. A System Events "window 1"
is whichever is frontmost, so an aux window in front used to be mistaken for the
watch and screenshotted in its place.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: The device window's title prefix. Everything else is not the watch.
DEVICE_TITLE_PREFIX = "CIQ Simulator"

#: Field separator in the window listing the AppleScript prints.
FS = "\x1f"
# Record separator for scans: alert bodies contain newlines, so a linefeed cannot end a record.
RS = "\x1e"

# Titles of windows that are alerts the dialog dismisser already owns, not
# auxiliary windows to close (closing "Error" with the close button does not
# release the app-modal block; its OK button does).
_DIALOG_SUBROLES = ("AXDialog", "AXSystemDialog", "AXFloatingWindow")


@dataclass
class Win:
    name: str
    subrole: str = ""
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    buttons: tuple = ()
    index: int = 0              # 1-based position in System Events' window list

    @property
    def is_device(self) -> bool:
        return self.name.startswith(DEVICE_TITLE_PREFIX)

    @property
    def is_dialog(self) -> bool:
        return self.subrole in _DIALOG_SUBROLES or "OK" in self.buttons


def parse_windows(text: str) -> list[Win]:
    """One window per line: ``name FS subrole FS x FS y FS w FS h FS btn,btn``."""
    out = []
    for i, line in enumerate((text or "").splitlines(), start=1):
        parts = line.split(FS)
        if len(parts) < 6:
            continue
        def num(s):
            try:
                return int(float(s))
            except ValueError:
                return 0
        btns = tuple(b for b in (parts[6].split(",") if len(parts) > 6 else []) if b)
        out.append(Win(parts[0], parts[1], num(parts[2]), num(parts[3]),
                       num(parts[4]), num(parts[5]), btns, i))
    return out


@dataclass
class Classified:
    device: Win | None = None
    aux: list = field(default_factory=list)
    dialogs: list = field(default_factory=list)


def classify(wins: list[Win]) -> Classified:
    """Split the simulator's windows into the device, dialogs and aux windows.

    The device window is the one titled like the simulator's main window; when
    none is (a localised build, a renamed title) the largest non-dialog window
    stands in, because the watch window is always bigger than any panel.
    """
    c = Classified()
    rest = []
    for w in wins:
        if w.is_device and c.device is None:
            c.device = w
        elif w.is_dialog:
            c.dialogs.append(w)
        else:
            rest.append(w)
    if c.device is None and rest:
        big = max(rest, key=lambda w: w.w * w.h)
        c.device = big
        rest = [w for w in rest if w is not big]
    c.aux = rest
    return c


# ── macOS crash-reporter dialogs ─────────────────────────────────────────────
#: Process names whose "quit unexpectedly" dialog is ours to dismiss. The
#: simulator is "simulator" (bundle ConnectIQ.app, so the dialog says
#: "ConnectIQ quit unexpectedly"); the rest are its helpers and the toolchain.
SIM_PROCESS_RE = re.compile(
    r"connect\s*iq|connectiq|simulator|monkeydo|monkeybrains|garmin|"
    r"webkit|web content|webpage", re.I)

_CRASH_RE = re.compile(
    r"[“\"']?(?P<proc>[^”\"'\n]+?)[”\"']?\s+"
    r"(?:quit unexpectedly|unexpectedly quit|closed because of a problem|"
    r"is not responding)", re.I)


def match_crash_dialog(texts) -> str | None:
    """The crashed process named by a crash-reporter dialog's text, if it is
    simulator-related; None for anything else (never touch other apps' dialogs)."""
    for t in texts:
        m = _CRASH_RE.search(t or "")
        if m:
            proc = m.group("proc").strip()
            # "Connect IQ quit unexpectedly." / "The application “X” quit ..."
            proc = re.sub(r"^(the )?(application|app)\s+", "", proc, flags=re.I).strip(" “”\"'")
            if SIM_PROCESS_RE.search(proc):
                return proc
    return None


# ── phone connection (Settings > Connection Type > BLE) ──────────────────────
PHONE_STATES = {
    "connected": "Connected", "on": "Connected", "true": "Connected",
    "disconnected": "Not Connected", "not_connected": "Not Connected",
    "not-connected": "Not Connected", "off": "Not Connected", "false": "Not Connected",
}


def phone_menu_item(state) -> str:
    """The BLE submenu item for ``state`` (bool or a name); ValueError if unknown."""
    if isinstance(state, bool):
        return "Connected" if state else "Not Connected"
    key = str(state).strip().lower()
    if key not in PHONE_STATES:
        raise ValueError(f"unknown phone connection {state!r}; use connected or "
                         f"disconnected")
    return PHONE_STATES[key]


def parse_marked_item(text: str) -> str | None:
    """The checked item in ``name FS mark`` lines (mark is a tick), or None."""
    for line in (text or "").splitlines():
        name, _, mark = line.partition(FS)
        if mark.strip() and mark.strip() != "missing value":
            return name.strip()
    return None


# ── FIT / GPX playback (Simulation > Activity Data) ──────────────────────────
#: The Activity Data window has no title, so it is found by its "Data Source"
#: label. Its data-source popup offers these entries (SDK 9.1.0).
FIT_SOURCE_ITEM = "FIT/GPX Playable File"

#: AppleScript handler: the 1-based index of the Activity Data window, or 0.
#: Re-run before every action -- opening the popup or a file panel reorders
#: System Events' window list, so a reference to "window N" goes stale.
_FIT_HANDLER = '''
on winIx()
  tell application "System Events" to tell process "simulator"
    set n to count of windows
    repeat with i from 1 to n
      try
        if exists static text "Data Source" of window i then return i
      end try
    end repeat
  end tell
  return 0
end winIx
'''

#: Opens the window (if needed), picks the FIT/GPX source, drives the file
#: panel's "Go to the folder" sheet to the file and confirms. argv: the path.
FIT_LOAD_SCRIPT = (_FIT_HANDLER +
                   'on run argv\n'
                   'tell application "System Events" to tell process "simulator"\n'
                   '  set frontmost to true\n'
                   '  if my winIx() is 0 then\n'
                   '    click menu item "Activity Data" of menu 1 of menu bar item "Simulation" of menu bar 1\n'
                   '    delay 1.2\n'
                   '  end if\n'
                   '  if my winIx() is 0 then return "ERR no Activity Data window"\n'
                   f'  if (value of pop up button 1 of window (my winIx())) is not "{FIT_SOURCE_ITEM}" then\n'
                   '    set opened to false\n'
                   '    repeat with attempt from 1 to 2\n'
                   '      click pop up button 1 of window (my winIx())\n'
                   '      repeat 15 times\n'
                   '        delay 0.2\n'
                   '        try\n'
                   '          if exists menu 1 of pop up button 1 of window (my winIx()) then\n'
                   '            set opened to true\n'
                   '            exit repeat\n'
                   '          end if\n'
                   '        end try\n'
                   '      end repeat\n'
                   '      if opened then exit repeat\n'
                   '    end repeat\n'
                   '    if not opened then return "ERR source menu did not open"\n'
                   f'    click menu item "{FIT_SOURCE_ITEM}" of menu 1 of pop up button 1 of window (my winIx())\n'
                   '    delay 0.8\n'
                   '  end if\n'
                   '  click button "Load File" of window (my winIx())\n'
                   '  delay 1.3\n'
                   '  keystroke "g" using {command down, shift down}\n'
                   '  delay 0.8\n'
                   '  keystroke (item 1 of argv)\n'
                   '  delay 0.5\n'
                   '  key code 36\n'
                   '  delay 0.8\n'
                   '  key code 36\n'
                   '  delay 1.5\n'
                   '  return "OK"\n'
                   'end tell\n'
                   'end run')

#: Prints the window's texts (one per line). argv[1] "play" first presses the
#: play button (the one button with no name); "close" closes the window.
FIT_STATE_SCRIPT = (_FIT_HANDLER +
                    'on run argv\n'
                    'tell application "System Events" to tell process "simulator"\n'
                    '  set ix to my winIx()\n'
                    '  if ix is 0 then return "ERR no Activity Data window"\n'
                    '  set w to window ix\n'
                    '  if (item 1 of argv) is "play" then\n'
                    '    set pb to missing value\n'
                    '    repeat with b in (buttons of w)\n'
                    '      set nm to missing value\n'
                    '      set sr to missing value\n'
                    '      try\n'
                    '        set nm to name of b\n'
                    '      end try\n'
                    '      try\n'
                    '        set sr to subrole of b\n'
                    '      end try\n'
                    '      if nm is missing value and sr is missing value then set pb to b\n'
                    '    end repeat\n'
                    '    if pb is missing value then return "ERR no play button"\n'
                    '    click pb\n'
                    '    delay 0.4\n'
                    '  end if\n'
                    '  if (item 1 of argv) is "close" then\n'
                    '    click (first button of w whose subrole is "AXCloseButton")\n'
                    '    return "OK"\n'
                    '  end if\n'
                    '  set out to "SRC=" & (value of pop up button 1 of w) & linefeed\n'
                    '  repeat with t in static texts of w\n'
                    '    set v to missing value\n'
                    '    try\n'
                    '      set v to value of t\n'
                    '    end try\n'
                    '    if v is not missing value then set out to out & "TXT=" & v & linefeed\n'
                    '  end repeat\n'
                    '  return out\n'
                    'end tell\n'
                    'end run')

_CLOCK_RE = re.compile(r"^(\d\d):(\d\d):(\d\d)(?:\s*/\s*(\d\d):(\d\d):(\d\d))?$")


def _secs(h, m, s) -> int:
    return int(h) * 3600 + int(m) * 60 + int(s)


def parse_fit_state(text: str) -> dict:
    """The Activity Data window's state from FIT_STATE_SCRIPT's output:
    ``{source, file, elapsed_s, total_s}`` (missing parts are None), or
    ``{error}`` when the script reported one."""
    out = {"source": None, "file": None, "elapsed_s": None, "total_s": None}
    t = (text or "").strip()
    if t.startswith("ERR"):
        return {"error": t[3:].strip() or "unknown error"}
    for line in t.splitlines():
        if line.startswith("SRC="):
            out["source"] = line[4:].strip()
        elif line.startswith("TXT="):
            v = line[4:].strip()
            m = _CLOCK_RE.match(v)
            if m:
                out["elapsed_s"] = _secs(*m.group(1, 2, 3))
                if m.group(4) is not None:
                    out["total_s"] = _secs(*m.group(4, 5, 6))
            elif v.lower().endswith((".fit", ".gpx")):
                out["file"] = v
    return out


# ── window-server records (macOS CGWindowListCopyWindowInfo) ────────────────
#
# A region grab reads whatever is on screen at that rect, so another app's
# window over the simulator ends up in the PNG (2026-11-19: macOS's "out of
# application memory" panel sat on top of every capture for hours). These pure
# helpers pick the simulator's own window id -- which lets the grab read that
# window's drawing alone -- and name whatever is covering it.

#: An overlap counts when it is at least this fraction of the window AND this
#: many logical px^2. Below that it is a sliver (a cursor-sized overlay, a
#: shadow edge) that cannot hide the watch screen.
OCCLUDER_MIN_FRAC = 0.01
OCCLUDER_MIN_AREA = 400.0


def _bounds(info: dict):
    b = info.get("kCGWindowBounds") or {}
    try:
        return (float(b["X"]), float(b["Y"]), float(b["Width"]), float(b["Height"]))
    except (KeyError, TypeError, ValueError):
        return None


def pick_window_id(infos, rect, owner: str = "simulator", tol: float = 3.0):
    """The CGWindowID of the simulator's device window, or None.

    ``rect`` is the (x, y, w, h) of the window ``find_sim_window`` found; the
    match is owner name plus bounds within ``tol`` px, normal layer only, so it
    is the same window and not an aux panel. Several candidates: the one titled
    like the device window wins, then the closest bounds.
    """
    x, y, w, h = rect
    best = None
    best_key = None
    for info in infos or []:
        if owner.lower() not in str(info.get("kCGWindowOwnerName", "")).lower():
            continue
        if info.get("kCGWindowLayer", 0) != 0:
            continue
        b = _bounds(info)
        wid = info.get("kCGWindowNumber")
        if b is None or wid is None:
            continue
        dist = max(abs(b[0] - x), abs(b[1] - y), abs(b[2] - w), abs(b[3] - h))
        if dist > tol:
            continue
        titled = str(info.get("kCGWindowName", "")).startswith(DEVICE_TITLE_PREFIX)
        key = (not titled, dist)
        if best_key is None or key < best_key:
            best, best_key = int(wid), key
    return best


def find_occluders(infos_above, rect, min_frac: float = OCCLUDER_MIN_FRAC,
                   min_area: float = OCCLUDER_MIN_AREA,
                   displays=()) -> list[dict]:
    """Windows from ``infos_above`` (on screen, above the simulator window)
    that cover a meaningful part of ``rect``: ``[{owner, title, area}]``,
    biggest overlap first. Invisible (alpha 0), empty and sliver-sized windows
    are ignored."""
    x, y, w, h = rect
    total = max(w * h, 1.0)
    out = []
    # The Dock and Notification Center each keep a transparent window the size
    # of the whole display above everything (measured macOS 26); it hides
    # nothing. A real alert or banner is never display-sized.
    for info in infos_above or []:
        try:
            if float(info.get("kCGWindowAlpha", 1.0)) <= 0.01:
                continue
        except (TypeError, ValueError):
            continue
        if info.get("kCGWindowIsOnscreen") is False:
            continue
        b = _bounds(info)
        if b is None or b[2] <= 0 or b[3] <= 0:
            continue
        if info.get("kCGWindowLayer", 0) != 0 and any(
                b[2] >= dw - 2 and b[3] >= dh - 2 for dw, dh in displays):
            continue
        ix = min(x + w, b[0] + b[2]) - max(x, b[0])
        iy = min(y + h, b[1] + b[3]) - max(y, b[1])
        if ix <= 0 or iy <= 0:
            continue
        area = ix * iy
        if area < min_area or area / total < min_frac:
            continue
        out.append({"owner": str(info.get("kCGWindowOwnerName", "") or "?"),
                    "title": str(info.get("kCGWindowName", "") or ""),
                    "area": round(area)})
    out.sort(key=lambda o: -o["area"])
    return out


def describe_occluders(occ) -> str:
    """``Owner: title, Owner2`` for a warning or error message."""
    return ", ".join(o["owner"] + (f": {o['title']}" if o.get("title") else "")
                     for o in occ)


# ── memory pressure ─────────────────────────────────────────────────────────
PRESSURE_NORMAL, PRESSURE_WARN, PRESSURE_CRITICAL = 1, 2, 4


def parse_pressure(text) -> int | None:
    """``kern.memorystatus_vm_pressure_level`` output -> 1/2/4, else None."""
    try:
        v = int(str(text).strip())
    except (TypeError, ValueError):
        return None
    return v if v in (1, 2, 4) else None
