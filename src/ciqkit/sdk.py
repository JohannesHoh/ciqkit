"""Where the Connect IQ SDK and its device profiles live on this machine.

Stdlib-only and GUI-free, apart from ``sim/`` on purpose: ``build.py`` and
``capture/framing.py`` need these paths, and importing them from
``sim.preflight`` made the lower layers depend on the broker's package.
``sim.preflight`` re-imports the names, so ``preflight.detect_sdk`` keeps working.
"""
from __future__ import annotations

import os
import re
import sys

# Where Connect IQ SDKs are installed by the SDK Manager, per platform.
_SDK_ROOTS = {
    "darwin": "~/Library/Application Support/Garmin/ConnectIQ/Sdks",
    "win32": "~/AppData/Roaming/Garmin/ConnectIQ/Sdks",
}


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


def detect_sdk(base: str | None = None) -> str:
    """Path to the newest installed Connect IQ SDK, or "".

    ``CIQ_HOME`` overrides the search entirely, matching the environment
    variable the Garmin SDK tooling itself uses. ``base`` replaces the SDK
    directory searched (``sdk_root()`` by default).
    """
    override = os.environ.get("CIQ_HOME")
    if override:
        # normpath: `~/sdk` expands to `C:\Users\me/sdk` on Windows.
        return os.path.normpath(os.path.expanduser(override))
    if base is None:
        base = sdk_root()
    if not base or not os.path.isdir(base):
        return ""
    sdks = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
    if not sdks:
        return ""
    return os.path.join(base, max(sdks, key=_version_key))
