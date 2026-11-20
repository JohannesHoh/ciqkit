"""Phone-connection and FIT/GPX playback control of the Connect IQ simulator (macOS).

Moved out of ``screenshot_utils`` and re-exported there. Every collaborator
(``IS_MAC``, ``subprocess``, ``time``, other moved helpers) is read through the
``screenshot_utils`` module at call time, so tests that monkeypatch ``su.*``
still take effect here and there is no import cycle.
"""
import os
import shutil
import tempfile
import time

from . import simwindows as _sw


def _su():
    from . import screenshot_utils
    return screenshot_utils


_BLE_MENU = ('tell menu 1 of menu bar item "Settings" of menu bar 1'
             ' to tell menu item "Connection Type" to tell menu 1'
             ' to tell menu item "BLE" to tell menu 1')


def read_phone_connection():
    """The checked item of Settings > Connection Type > BLE ("Connected",
    "Not Connected", "Not Initialized"), or None if unreadable / not macOS."""
    if not _su().IS_MAC:
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
        r = _su().subprocess.run(["osascript", "-e", script], capture_output=True,
                           text=True, timeout=6)
    except Exception:
        return None
    return _sw.parse_marked_item(r.stdout)


def set_phone_connection(state) -> dict:
    """Set the simulated phone link (macOS): Settings > Connection Type > BLE >
    Connected / Not Connected, then read the menu back. ``DeviceSettings.
    phoneConnected`` follows it. Returns {ok, requested, state, changed}."""
    want = _sw.phone_menu_item(state)
    if not _su().IS_MAC:
        return {"ok": False, "requested": want, "state": None,
                "error": "phone connection control is implemented for macOS only"}
    before = _su().read_phone_connection()
    if before != want:
        script = (
            'tell application "System Events" to tell (first process whose name is "simulator")\n'
            f'    {_BLE_MENU}\n'
            f'        click menu item "{want}"\n'
            '    end tell\n'
            'end tell')
        try:
            _su().subprocess.run(["osascript", "-e", script], capture_output=True,
                           text=True, timeout=6)
        except Exception:  # noqa: S110
            pass   # best effort: the read-back below reports whether the click landed
        _su().time.sleep(0.5)
    after = _su().read_phone_connection()
    return {"ok": after == want, "requested": want, "state": after,
            "changed": before != after, "previous": before}



def _fit_osascript(script: str, *args, timeout: float = 30) -> dict:
    r = _su().subprocess.run(["osascript", "-e", script, "--", *args], capture_output=True,
                       text=True, timeout=timeout)
    if r.returncode != 0:
        return {"error": (r.stderr or "osascript failed").strip()}
    return _sw.parse_fit_state(r.stdout)


def read_fit_playback() -> dict:
    """The Activity Data window's state ({source, file, elapsed_s, total_s}), or
    {error}. macOS only."""
    if not _su().IS_MAC:
        return {"error": "FIT playback control is implemented for macOS only"}
    return _su()._fit_osascript(_sw.FIT_STATE_SCRIPT, "state")


#: Where FIT/GPX files are copied before the file panel loads them (see stage_fit_for_panel).
FIT_STAGE_DIR = os.path.join(os.path.expanduser("~"), "Library", "Caches", "ciqkit", "fit")

#: Staged copies older than this are removed on the next call; nothing else cleans them up.
FIT_STAGE_MAX_AGE_S = 24 * 3600


def _prune_fit_stage(stage_dir: str, now: float) -> None:
    for name in os.listdir(stage_dir):
        sub = os.path.join(stage_dir, name)
        try:
            if os.path.islink(sub) or now - os.lstat(sub).st_mtime < FIT_STAGE_MAX_AGE_S:
                continue
            shutil.rmtree(sub, ignore_errors=True)
        except OSError:
            pass   # best-effort housekeeping: a stale copy must not fail a load


def stage_fit_for_panel(path: str, stage_dir: str = None) -> str:
    """The path to type into the file panel's Go-to-folder sheet for ``path``.

    The panel will not open a file under the per-user temp dir (``/var/folders/..``,
    where ``tempfile``/``os.tmpdir()`` put files, either spelling): the sheet takes
    the path but the window stays empty (2026-11-12, SDK 9.1.0; a home or ``/tmp``
    path loads). Such a file is copied to ``FIT_STAGE_DIR`` and that copy is loaded;
    any other path is returned unchanged.

    The copy keeps its basename (the playback window shows it, and the caller
    compares it) but sits in a fresh per-call subdirectory, so two files of the same
    name never overwrite each other. The stage dir is private (0700, ours, not a
    symlink -- it holds the user's activity data) and the copy is 0600; copies over
    a day old are removed here since nothing else would.
    """
    real = os.path.realpath(path)
    if not (real.startswith("/private/var/folders/") or real.startswith("/var/folders/")):
        return path
    from ..paths import _ensure_private
    stage_dir = _ensure_private(stage_dir or FIT_STAGE_DIR)
    _prune_fit_stage(stage_dir, time.time())
    sub = tempfile.mkdtemp(prefix="f-", dir=stage_dir)   # mkdtemp is created 0700
    dst = os.path.join(sub, os.path.basename(path))
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out, open(real, "rb") as src:
        shutil.copyfileobj(src, out)
    return dst


def start_fit_playback(path: str, advance_s: float = 12.0,
                       close_window: bool = True) -> dict:
    """Simulation > Activity Data > FIT/GPX Playable File: load ``path`` and press
    play, then VERIFY it: the window must show that file, and its playback clock
    must have advanced. ``ok`` is False otherwise, with a ``reason``.

    Returns {ok, file, total_s, elapsed_s, reason?}. macOS only.
    """
    path = os.path.abspath(os.path.expanduser(path))
    res = {"ok": False, "file": path}
    if not _su().IS_MAC:
        res["reason"] = "FIT playback control is implemented for macOS only"
        return res
    if not os.path.isfile(path):
        res["reason"] = f"no such file: {path}"
        return res
    path = _su().stage_fit_for_panel(path)
    try:
        r = _su().subprocess.run(["osascript", "-e", _sw.FIT_LOAD_SCRIPT, "--", path],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or r.stdout.strip().startswith("ERR"):
            res["reason"] = (r.stdout or r.stderr).strip() or "load script failed"
            return res
        st = _su()._fit_osascript(_sw.FIT_STATE_SCRIPT, "state")
        if st.get("error"):
            res["reason"] = st["error"]
            return res
        if st.get("file") != path:
            res["reason"] = (f"the playback window shows {st.get('file')!r}, not the "
                             f"requested file (the file panel did not take the path)")
            return res
        res["total_s"] = st.get("total_s")
        st = _su()._fit_osascript(_sw.FIT_STATE_SCRIPT, "play")
        deadline = _su().time.monotonic() + advance_s
        while not st.get("error") and not (st.get("elapsed_s") or 0) > 0:
            if _su().time.monotonic() >= deadline:
                break
            _su().time.sleep(0.7)
            st = _su()._fit_osascript(_sw.FIT_STATE_SCRIPT, "state")
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
            _su()._fit_osascript(_sw.FIT_STATE_SCRIPT, "close")
    except _su().subprocess.TimeoutExpired:
        res["reason"] = "System Events did not answer while driving the Activity Data window"
    return res
