"""Permission, SDK and environment preflight.

Turns the environment failures that otherwise present as a mysterious hang into
one actionable message:

  1. Platform -- the Connect IQ Simulator is macOS/Windows only.
  2. SDK layout -- recent SDKs ship `bin/ConnectIQ.app`; older ones a
     `bin/simulator` binary.
  3. Accessibility -- without the macOS Accessibility grant, System Events
     automation fails with osascript error -25211 ("not allowed assistive
     access"). Window lookup then returns nothing and it looks exactly like
     "the simulator did not start".
  4. Remote/headless -- the simulator aborts without a local WindowServer
     (VNC / Screen Sharing / SSH). We warn; only the user can fix that by
     running on the physical console.
  5. Windows -- Java, an interactive (unlocked) desktop, the simulator's own
     touch switch, and a Git Bash for shell-script builds; see
     :func:`_check_windows`.

:func:`check_once` memoizes a PASS, so a long-running daemon pays the cost
once. A FAILURE is never cached -- these are conditions the user fixes while
the daemon is running, and the next job must see the fix.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

_done = False
_result = None

# Where Connect IQ SDKs are installed by the SDK Manager, per platform.
_SDK_ROOTS = {
    "darwin": "~/Library/Application Support/Garmin/ConnectIQ/Sdks",
    "win32": "~/AppData/Roaming/Garmin/ConnectIQ/Sdks",
}


class PreflightError(RuntimeError):
    pass


def _garmin_home() -> str:
    """``.../Garmin/ConnectIQ`` for this platform ("" if unknown).

    On Windows %APPDATA% is asked for rather than assumed to be
    ``~/AppData/Roaming``: a roaming profile or folder redirection moves it.
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(appdata, "Garmin", "ConnectIQ")
    root = _SDK_ROOTS.get(sys.platform)
    return os.path.dirname(os.path.expanduser(root)) if root else ""


def sdk_root() -> str:
    """Directory holding installed SDK versions ("" if the platform is unknown)."""
    home = _garmin_home()
    return os.path.join(home, "Sdks") if home else ""


def devices_root() -> str:
    """Directory holding the SDK Manager's device profiles (``<id>/<id>.png``,
    ``compiler.json``); "" if the platform is unknown."""
    home = _garmin_home()
    return os.path.join(home, "Devices") if home else ""


def _version_key(name: str):
    """Sort key that orders SDK directory names by version, not alphabetically.

    Directories look like ``connectiq-sdk-mac-9.10.0-2026-01-30-abcdef``. Plain
    string sorting puts 9.9.0 after 9.10.0, so "the newest SDK" silently became
    "the one whose name sorts last" the moment a two-digit minor shipped. Runs
    of digits compare numerically; everything else compares as text.
    """
    parts = re.findall(r"\d+|\D+", name)
    return [(1, int(p), "") if p.isdigit() else (0, 0, p) for p in parts]


def detect_sdk() -> str:
    """Path to the newest installed Connect IQ SDK, or "".

    ``CIQ_HOME`` overrides the search entirely, matching the environment
    variable the Garmin SDK tooling itself uses.
    """
    override = os.environ.get("CIQ_HOME")
    if override:
        # normpath: `~/sdk` expands to `C:\Users\me/sdk` on Windows.
        return os.path.normpath(os.path.expanduser(override))
    base = sdk_root()
    if not base or not os.path.isdir(base):
        return ""
    sdks = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
    if not sdks:
        return ""
    return os.path.join(base, max(sdks, key=_version_key))


def _check_platform(warnings):
    if sys.platform in ("darwin", "win32"):
        return
    raise PreflightError(
        f"Unsupported platform {sys.platform!r}. ciqkit drives the Connect IQ "
        f"Simulator on macOS and Windows only; a backend for Garmin's beta "
        f"Linux simulator is not built yet (feature requests welcome: "
        f"https://github.com/JohannesHoh/ciqkit/issues). The static checks "
        f"(layout, shot-score, icon) work here.")


def _sim_ini() -> dict:
    """The simulator's own settings (Windows: %APPDATA%\\Garmin\\ConnectIQ\\
    simulator.ini, plain key=value). {} when absent or unreadable."""
    path = os.path.join(_garmin_home(), "simulator.ini") if _garmin_home() else ""
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                k, sep, v = line.partition("=")
                if sep:
                    out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def _check_windows(warnings):
    """What stops the Windows simulator working, said up front.

    * Java: every push and test run is a java process (bin\\monkeydo.bat is a
      wrapper around one java line). Without it the simulator comes up, the
      push silently does nothing, and the load times out 40 s later.
    * An interactive desktop: the simulator is a GUI app, and gestures are
      real input. A service or a non-interactive scheduled task has no
      desktop at all; a LOCKED session still captures but drops every tap
      and key, which then reads as "the app did not react".
    * Touch: the simulator has its own "touch screen" switch
      (simulator.ini TouchScreenEnabled). Off, it ignores every tap and
      swipe however they are sent; button keys still work.
    * Git Bash: the default build command is a shell script.
    """
    if sys.platform != "win32":
        return
    from .simport import java_exe
    if not java_exe():
        raise PreflightError(
            "Java was not found (not on PATH, no JAVA_HOME). The Connect IQ "
            "tools run on Java -- monkeydo, the test runner and the compiler "
            "all need it -- so no app can be pushed to the simulator. Install a "
            "JDK (e.g. Eclipse Temurin) and make sure `java -version` works in "
            "a new terminal.")
    try:
        from .. import _winapi
        if _winapi.session_id() == 0:
            raise PreflightError(
                "ciqkit is running in session 0 (a Windows service), which has "
                "no desktop: the simulator cannot show a window there. Run it "
                "from a logged-in user session.")
        if _winapi.session_locked():
            warnings.append(
                "The Windows session is locked: screenshots, recordings and "
                "test runs still work, but taps, swipes and keys cannot reach "
                "the simulator until it is unlocked.")
    except PreflightError:
        raise
    except Exception:   # noqa: S110 -- a probe that cannot run is not a finding
        pass
    if _sim_ini().get("TouchScreenEnabled") == "0":
        warnings.append(
            "The simulator's touch screen is switched off (simulator.ini "
            "TouchScreenEnabled=0), so it ignores every tap and swipe -- only "
            "button keys reach the app. Turn it on in the simulator's Settings "
            "menu if your navigation uses touch.")
    if not os.environ.get("CIQKIT_BUILD_CMD"):
        from ..winbash import git_bash
        if not git_bash():
            warnings.append(
                "No Git for Windows bash found, so the default build command "
                "(./scripts/build.sh) cannot run; ciqkit will fall back to a "
                "scripts\\build.cmd/.bat/.ps1 if there is one. Pass --prg, or set "
                "CIQKIT_BUILD_CMD.")


def _check_sdk(warnings):
    sdk = detect_sdk()
    if not sdk:
        raise PreflightError(
            "No Connect IQ SDK found"
            + (f" under {sdk_root()}" if sdk_root() else "")
            + ". Install one with the Connect IQ SDK Manager "
              "(https://developer.garmin.com/connect-iq/sdk/), or set CIQ_HOME "
              "to an SDK directory.")
    app = os.path.join(sdk, "bin", "ConnectIQ.app")
    legacy = os.path.join(sdk, "bin", "simulator")
    legacy_exe = legacy + ".exe"
    if not (os.path.isdir(app) or os.path.exists(legacy) or os.path.exists(legacy_exe)):
        raise PreflightError(
            f"{sdk} does not look like a Connect IQ SDK: no ConnectIQ.app or "
            f"simulator binary in {os.path.join(sdk, 'bin')}")
    return sdk


def _check_accessibility(warnings):
    """Probe System Events for WINDOW access (not just process enumeration).

    The subtle trap this fixes: `get name of ... process` needs no Accessibility
    grant and passes even when the grant is missing — but reading a process's
    *windows* (exactly what find_sim_window does) raises -25211. So we must probe
    window access, or preflight green-lights a capture that then dies with the
    misleading 'simulator did not start'."""
    if sys.platform != "darwin":
        return
    probe = ('tell application "System Events" to get size of '
             'front window of (first process whose frontmost is true)')
    r = subprocess.run(["osascript", "-e", probe], capture_output=True, text=True)
    err = (r.stderr or "")
    if "-25211" in err or "not allowed assistive access" in err.lower():
        raise PreflightError(
            "macOS Accessibility permission is missing (osascript -25211).\n"
            "  Window control and taps will silently fail, and the symptom looks\n"
            "  exactly like 'the simulator did not start'.\n"
            "  Grant it: System Settings -> Privacy & Security -> Accessibility.\n"
            "  Enable BOTH the terminal or editor that runs this process AND\n"
            "  /usr/bin/osascript, then fully quit and reopen that app.")
    # Any other rc (e.g. no front window right now) is not an Accessibility fail.


def _check_remote(warnings):
    if sys.platform != "darwin":
        return
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        warnings.append(
            "Running under SSH: the simulator needs a local WindowServer and "
            "aborts on a headless session. Run on the physical Mac console for "
            "reliable capture.")


def check_once() -> dict:
    """Run the preflight, memoizing only a PASS.

    A failure is deliberately NOT cached. Every condition checked here is one
    the user fixes while the daemon is running -- granting Accessibility in
    System Settings, installing an SDK, moving off a VNC session -- and caching
    the failure meant they then had to work out that the fix had not taken
    because a background process was still holding a three-minute-old "no". The
    broker lives for ten idle minutes, so that is ten minutes of every job
    failing for a reason that has already been dealt with.

    Re-running costs a couple of subprocesses, and only on a path that is
    already failing.
    """
    global _done, _result
    if _done:
        return _result
    warnings = []
    _check_platform(warnings)
    sdk = _check_sdk(warnings)
    _check_accessibility(warnings)
    _check_remote(warnings)
    _check_windows(warnings)
    # Only past every check do we memoize: a PreflightError propagates from the
    # calls above with _done still False, so the next call retries.
    _done = True
    _result = {"ok": True, "sdk": sdk, "warnings": warnings}
    for w in warnings:
        print(f"[ciqkit preflight] WARN: {w}", file=sys.stderr)
    return _result


def reset():
    """Testing hook — clear the memoized result."""
    global _done, _result
    _done, _result = False, None
