"""Private, per-user runtime directories.

Everything ciqkit creates outside the user's project lives here: the broker's
control channel, lease files, default screenshot output.

Why this module exists at all
-----------------------------
The broker accepts jobs over a local control channel, and a job can build a
project (running its ``build.sh``) and patch source files. That makes the
channel a command channel. A socket sitting in a shared, world-writable
``/tmp`` with default permissions is therefore a local code-execution vector on
any multi-user machine: another account can connect and submit jobs, or
pre-create the directory and hijack the path.

So every runtime path is:

  * per-user      -- POSIX: the directory name includes the uid. Windows: it
    lives in the user's own profile (``%LOCALAPPDATA%``);
  * private       -- POSIX: mode 0700, created with a restrictive umask, then
    chmod'ed. Windows: a protected DACL granting only this user and SYSTEM;
  * ownership-checked -- if the directory exists but is not owned by us (or,
    on POSIX, is group/other-accessible and cannot be tightened), we refuse
    rather than use it.

``XDG_RUNTIME_DIR`` is preferred when set (it is already a private per-user
tmpfs on Linux); otherwise POSIX falls back to ``<tmp>/ciqkit-<uid>`` and
Windows to ``%LOCALAPPDATA%\\ciqkit\\runtime``.
"""
from __future__ import annotations

import errno
import os
import stat
import sys
import tempfile

__all__ = ["runtime_dir", "state_dir", "default_out_dir", "InsecureRuntimeDir",
           "UnsupportedPlatform"]

IS_WIN = sys.platform == "win32"


class InsecureRuntimeDir(RuntimeError):
    """A runtime directory exists but is not a private directory we own."""


class UnsupportedPlatform(RuntimeError):
    """The platform has neither POSIX uids nor the Windows security APIs."""


def _require_supported():
    """Privacy is keyed on a POSIX uid, or on Windows on the user's SID.

    Without this the failure on anything else is an AttributeError on
    `os.getuid` from somewhere deep in the call stack, which reads like a bug
    rather than an unsupported platform.
    """
    if not IS_WIN and not hasattr(os, "getuid"):
        raise UnsupportedPlatform(
            "ciqkit's private runtime directory needs POSIX uids or the Windows "
            "security APIs, and this platform has neither. The simulator broker "
            "and `ciqkit lease` therefore do not run here. The static checks "
            "(`ciqkit layout`, `shot-score`, `icon`) work everywhere.")


def _base_dir() -> str:
    _require_supported()
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and os.path.isdir(xdg):
        return os.path.join(xdg, "ciqkit")
    if IS_WIN:
        # The user's own profile, which other non-admin accounts cannot even
        # list -- unlike a temp dir, which a TEMP/TMP override can point at
        # something shared.
        local = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return os.path.join(local, "ciqkit", "runtime")
    return os.path.join(tempfile.gettempdir(), f"ciqkit-{os.getuid()}")


def _ensure_private_windows(path: str) -> str:
    """The Windows half of :func:`_ensure_private`.

    A reparse point (symlink or junction) is refused rather than followed, the
    owner must be this user, and the DACL is (re)written to "this user and
    SYSTEM only" on every call -- cheap, and it means a directory someone
    loosened by hand is put back rather than trusted.
    """
    from . import _winapi
    st = os.lstat(path)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if getattr(st, "st_file_attributes", 0) & reparse or not stat.S_ISDIR(st.st_mode):
        raise InsecureRuntimeDir(
            f"{path} exists but is not a plain directory (a symlink or junction?). "
            f"Remove it and retry.")
    mine, owner = _winapi.owned_by_me(path)
    if not mine:
        raise InsecureRuntimeDir(
            f"{path} is owned by {owner}, not by you ({_winapi.user_sid()}). "
            f"Refusing to use it. Remove it, or set XDG_RUNTIME_DIR to a "
            f"directory you own.")
    try:
        _winapi.make_private(path)
    except OSError as exc:
        raise InsecureRuntimeDir(
            f"{path} could not be restricted to your account ({exc}). Refusing "
            f"to keep the broker's control channel in it.") from exc
    return path


def _ensure_private(path: str) -> str:
    """Create ``path`` as a private directory we own, or fail loudly.

    Uses ``os.mkdir`` (not ``makedirs(exist_ok=True)``) for the leaf so an
    existing entry is always inspected rather than silently trusted, and
    ``lstat`` so a symlink pointing somewhere else is rejected instead of
    followed.
    """
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, mode=0o700, exist_ok=True)
    try:
        os.mkdir(path, 0o700)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise

    if IS_WIN:
        return _ensure_private_windows(path)

    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise InsecureRuntimeDir(
            f"{path} exists but is not a directory (symlink attack?). "
            f"Remove it and retry.")
    if st.st_uid != os.getuid():
        raise InsecureRuntimeDir(
            f"{path} is owned by uid {st.st_uid}, not by you (uid {os.getuid()}). "
            f"Refusing to use it. Remove it, or set XDG_RUNTIME_DIR to a "
            f"directory you own.")
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        # Tighten rather than fail: this is usually just an inherited umask.
        os.chmod(path, 0o700)
        st = os.lstat(path)
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise InsecureRuntimeDir(
                f"{path} is accessible to other users and could not be "
                f"tightened to 0700.")
    return path


def runtime_dir() -> str:
    """Private dir for the control channel and lock files. Created on first use."""
    return _ensure_private(_base_dir())


def state_dir(name: str) -> str:
    """A private sub-directory of :func:`runtime_dir`, e.g. ``leases``."""
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        raise ValueError(f"invalid state dir name: {name!r}")
    return _ensure_private(os.path.join(runtime_dir(), name))


def default_out_dir() -> str:
    """Where screenshots land when the caller does not pass ``--out-dir``.

    Deliberately *not* inside the user's project: ciqkit never writes into a
    git working tree unless explicitly told to.
    """
    return _ensure_private(os.path.join(runtime_dir(), "shots"))


def privacy_note() -> str:
    """How the runtime dir is protected, for `ciqkit doctor`."""
    return "private: your account + SYSTEM only" if IS_WIN else "private, 0700"
