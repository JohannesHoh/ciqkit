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
                   '    click pop up button 1 of window (my winIx())\n'
                   '    delay 0.6\n'
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
