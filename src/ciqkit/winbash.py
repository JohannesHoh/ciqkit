"""Running a project's POSIX shell scripts on Windows.

ciqkit's default build command is ``./scripts/build.sh -d <device>`` and
``ciqkit lease`` is usually wrapped around a shell script, so on Windows those
need a bash. The right one is Git for Windows' (MSYS2): it sees the Windows
filesystem as ``/c/...`` and runs the SDK's ``.bat`` tools natively.

The WRONG one is first on PATH on a stock Windows 11 box:
``C:\\Windows\\System32\\bash.exe`` / ``...\\WindowsApps\\bash.exe`` is the WSL
launcher, which runs the script inside a Linux VM with a different filesystem,
no access to the Windows SDK's ``.bat`` files and no simulator. So this never
asks PATH for "bash"; it finds Git and takes the bash that ships beside it.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

from ._compat import hidden_console_kwargs

__all__ = ["git_bash", "to_windows_path"]

_cache: dict = {}


def _git_roots():
    git = shutil.which("git")
    if git:
        # <root>\cmd\git.exe, <root>\bin\git.exe or <root>\mingw64\bin\git.exe
        d = os.path.dirname(os.path.realpath(git))
        for up in (1, 2):
            root = d
            for _ in range(up):
                root = os.path.dirname(root)
            yield root
    for env in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if base:
            yield os.path.join(base, "Git")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        yield os.path.join(local, "Programs", "Git")


def git_bash() -> str | None:
    """Path to Git for Windows' bash.exe, or None. ``CIQKIT_BASH`` overrides."""
    override = os.environ.get("CIQKIT_BASH")
    if override:
        return override
    if "bash" in _cache:
        return _cache["bash"]
    found = None
    for root in _git_roots():
        for rel in (("bin", "bash.exe"), ("usr", "bin", "bash.exe")):
            cand = os.path.join(root, *rel)
            if os.path.isfile(cand):
                found = cand
                break
        if found:
            break
    _cache["bash"] = found
    return found


_DRIVE = re.compile(r"^/([a-zA-Z])(/|$)")


def to_windows_path(path: str) -> str:
    """Translate a path an MSYS bash printed into one Windows can open.

    ``/c/Users/x/bin/App.prg`` is handled here; anything else under the MSYS
    root (``/tmp/...``, which Git Bash maps onto %TEMP%) is asked of
    ``cygpath``. Paths that are already Windows paths come back unchanged.
    """
    if not path.startswith("/"):
        return path
    m = _DRIVE.match(path)
    if m:
        rest = path[m.end():]
        return f"{m.group(1).upper()}:\\" + rest.replace("/", "\\")
    bash = git_bash()
    if bash:
        cygpath = os.path.join(os.path.dirname(bash), "cygpath.exe")
        if not os.path.isfile(cygpath):
            cygpath = os.path.join(os.path.dirname(os.path.dirname(bash)),
                                   "usr", "bin", "cygpath.exe")
        if os.path.isfile(cygpath):
            try:
                r = subprocess.run([cygpath, "-w", path], capture_output=True,
                                   text=True, timeout=10, **hidden_console_kwargs())
                if r.returncode == 0 and r.stdout.strip():
                    return r.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
    return path
