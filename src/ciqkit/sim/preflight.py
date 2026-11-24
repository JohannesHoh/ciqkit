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

import json
import os
import subprocess
import sys

# Re-exported: callers and tests reach these through ``preflight``.
from .. import sdk as _sdk
from ..sdk import _garmin_home, _version_key, devices_root, sdk_root  # noqa: F401

_done = False
_result = None

class PreflightError(RuntimeError):
    pass


def detect_sdk() -> str:
    """See :func:`ciqkit.sdk.detect_sdk`. Resolves ``sdk_root`` through this
    module so a test (or caller) that patches ``preflight.sdk_root`` still
    steers detection."""
    return _sdk.detect_sdk(sdk_root())


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
    try:
        r = subprocess.run(["osascript", "-e", probe], capture_output=True, text=True,
                           timeout=60)
    except subprocess.TimeoutExpired:
        raise PreflightError(
            "macOS did not answer within 60 s whether ciqkit may control windows; "
            "a permission dialog may be waiting on the Mac's screen.") from None
    err = (r.stderr or "")
    if "-25211" in err or "not allowed assistive access" in err.lower():
        raise PreflightError(
            "macOS Accessibility permission is missing (osascript -25211).\n"
            "  Window control and taps will silently fail, and the symptom looks\n"
            "  exactly like 'the simulator did not start'.\n"
            "  Grant it: System Settings -> Privacy & Security -> Accessibility.\n"
            + ("  Switch on ciqkit there."
               if os.environ.get("CIQKIT_APP") == "1" else
               "  Enable BOTH the terminal or editor that runs this process AND\n"
               "  /usr/bin/osascript, then fully quit and reopen that app.\n"
               "  Or run `ciqkit mac-app install`, so ciqkit is listed by its own "
               "name."))
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


def report(request: bool = False) -> dict:
    """The preflight verdict for THIS process, plus macOS' own permission answers.

    Always fresh: someone asking wants to know whether the switch they just
    flipped has taken."""
    out = {"ok": True, "as_app": os.environ.get("CIQKIT_APP") == "1"}
    if sys.platform == "darwin":
        from .. import macperm_cli
        argv = [sys.executable, "-I", "-m", macperm_cli.__name__]
        try:
            r = subprocess.run(argv + (["--request"] if request else []),
                               capture_output=True, text=True, timeout=30,
                               stdin=subprocess.DEVNULL)
            out.update(json.loads(r.stdout))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    reset()
    try:
        out.update(passed=True, warnings=check_once().get("warnings", []))
    except PreflightError as exc:
        out.update(passed=False, error=str(exc))
    return out


def reset():
    """Testing hook — clear the memoized result."""
    global _done, _result
    _done, _result = False, None


def memory_pressure() -> int | None:
    """macOS memory-pressure level: 1 normal, 2 warn, 4 critical; None when it
    cannot be read (non-macOS, sysctl missing or odd output). Cheap (~5 ms) and
    never raises -- an unreadable level means "no check", not a failed job."""
    if sys.platform != "darwin":
        return None
    try:
        r = subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                           capture_output=True, text=True, timeout=2)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    from ..capture.simwindows import parse_pressure
    return parse_pressure(r.stdout)


def pressure_verdict(level) -> tuple[str, str] | None:
    """(``"fail"`` | ``"warn"``, message) for a pressure level, or None."""
    if level == 4:
        return ("fail", "macOS memory pressure is CRITICAL: the system may show "
                "its 'out of application memory' dialog over the simulator "
                "and the job's screens would be unusable. Quit memory-heavy "
                "apps and retry.")
    if level == 2:
        return ("warn", "macOS memory pressure is elevated (warn): the "
                "'out of application memory' dialog may appear over the "
                "simulator; check shots for it.")
    return None
