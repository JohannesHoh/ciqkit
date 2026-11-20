"""Run a project's build command and locate the .prg it produced.

Split out of ``session.py``: nothing here touches the simulator, a window or
the capture layer, so the build step is testable and importable on its own.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time

from .. import _compat
from .. import build as _build
from .._compat import hidden_console_kwargs
from .errors import SimError

# Where build scripts in the wild drop their .prg, searched in this order when
# the build's own output does not name the file.
BUILD_OUTPUT_DIRS = ("bin", "build", "gen", ".")


def _announced_prg(output: str, app_dir: str, since: float) -> str | None:
    """The .prg path a build script printed, or None.

    Build scripts announce their output in whatever format they like -- "Build
    successful: x.prg", "BUILD SUCCESSFUL -> x.prg", "Wrote x.prg" -- so any
    token ending in .prg is a candidate. That is loose on purpose, which is
    exactly why the candidate must also be a file written since this build
    started: scripts routinely mention .prg paths they did NOT just produce
    (an `rm -f bin/App-test.prg` line, a warning quoting last week's binary, a
    usage string). Taking the first one that merely *exists* is how a stale or
    unrelated build ends up in a screenshot -- the same failure the directory
    scan's mtime floor exists to prevent, so it applies here too.

    Ties go to the newest, since a script that names several is most likely
    naming its final output last.
    """
    fresh = []
    for m in re.finditer(r"(\S+\.prg)", output):
        cand = m.group(1).strip("\"'()[],")
        if sys.platform == "win32" and cand.startswith("/"):
            # A script run under Git Bash names its output the MSYS way,
            # /c/... or /tmp/..., which Windows cannot open as such.
            from ..winbash import to_windows_path
            cand = to_windows_path(cand)
        full = cand if os.path.isabs(cand) else os.path.join(app_dir, cand)
        try:
            if os.path.isfile(full) and os.path.getmtime(full) >= since:
                fresh.append(full)
        except OSError:
            continue
    return max(fresh, key=os.path.getmtime) if fresh else None


def _prg_written_since(app_dir: str, since: float) -> str | None:
    """Newest .prg under the usual output dirs that was written after `since`."""
    found = []
    for rel in BUILD_OUTPUT_DIRS:
        d = os.path.join(app_dir, rel)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.endswith(".prg"):
                continue
            path = os.path.join(d, name)
            try:
                if os.path.getmtime(path) >= since:
                    found.append(path)
            except OSError:
                continue
    return max(found, key=os.path.getmtime) if found else None


# What the default command means on Windows, tried in order: the same script
# under Git Bash, then a native script beside it. See _build_argv.
_WIN_NATIVE_SCRIPTS = ("build.cmd", "build.bat", "build.ps1")


def _build_argv(cmd: str, app_dir: str) -> list[str]:
    """Turn the build command into an argv this platform can execute.

    POSIX: shlex, as before.

    Windows needs three things POSIX does not:

      * a ``.sh`` script cannot be executed directly -- it is handed to Git for
        Windows' bash, never to the ``bash.exe`` on PATH, which on a stock
        Windows 11 box is the WSL launcher and would run the build inside a
        Linux VM that cannot see the Windows SDK (see ciqkit.winbash);
      * with the DEFAULT command and no Git Bash, a native
        ``scripts\\build.cmd`` / ``.bat`` / ``.ps1`` is used instead;
      * shlex is not used to split: it eats the backslashes of every Windows
        path in a CIQKIT_BUILD_CMD (``C:\\sdk\\bin\\monkeyc.bat`` became
        ``C:sdkbinmonkeyc.bat``). ``posix=False`` keeps them and quotes are
        stripped by hand.
    """
    if sys.platform != "win32":
        return shlex.split(cmd)
    argv = [a[1:-1] if len(a) >= 2 and a[0] == a[-1] and a[0] in "\"'" else a
            for a in shlex.split(cmd, posix=False)]
    if not argv:
        return argv
    script = argv[0]
    if script.lower().endswith(".sh"):
        from ..winbash import git_bash
        bash = git_bash()
        if bash:
            return [bash, script.replace("\\", "/"), *argv[1:]]
        scripts = os.path.normpath(
            os.path.join(app_dir, os.path.dirname(script.replace("/", os.sep))))
        for native in _WIN_NATIVE_SCRIPTS:
            cand = os.path.join(scripts, native)
            if os.path.isfile(cand):
                if native.endswith(".ps1"):
                    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", cand, *argv[1:]]
                return [cand, *argv[1:]]
        raise SimError(
            f"{script} is a shell script, and there is no Git for Windows bash "
            f"to run it with (the bash.exe on PATH, if any, is WSL's and cannot "
            f"see this SDK). Install Git for Windows, set CIQKIT_BASH to its "
            f"bash.exe, add a scripts\\build.cmd, or set CIQKIT_BUILD_CMD.")
    found = shutil.which(script, path=os.pathsep.join([app_dir, os.environ.get("PATH", "")]))
    if found and not os.path.isabs(script) and not script.startswith("."):
        argv[0] = found
    elif script.startswith(".") and os.path.isfile(os.path.join(app_dir, script)):
        # CreateProcess resolves a relative program against OUR working
        # directory, not the child's cwd, so `.\scripts\build.cmd` has to be
        # made absolute against the project.
        argv[0] = os.path.normpath(os.path.join(app_dir, script))
    return argv


def build(app_dir: str, device: str, timeout_s: float, build_cmd: str | None,
          tests: bool) -> str:
    """Run the project's build and return the built .prg path.

    See :meth:`SimSession.build` for the freshness rules; defaults live there.
    """
    app_dir = os.path.abspath(app_dir)
    if not os.path.isdir(app_dir):
        raise SimError(f"project directory not found: {app_dir}")
    try:
        builtin, template = _build.resolve_build(app_dir, device,
                                                 build_cmd=build_cmd,
                                                 tests=tests)
    except (_build.BuildError, ValueError) as exc:   # ValueError: ConfigError
        raise SimError(str(exc)) from exc
    if tests and builtin is None and template != _build.toml_test_cmd(app_dir):
        # A user build command has no placeholder for "with -t", so running
        # it would hand back a NORMAL build -- which contains no test cases
        # and would "pass" with zero tests. Refuse rather than guess.
        raise SimError(
            f"cannot build the unit-test variant: this project uses a custom "
            f"build command ({template}), which has no test variant. Pass "
            f"--prg with a build made by `monkeyc -t`, set `[build] test_cmd` "
            f"in ciqkit.toml, or remove the custom build command to use the "
            f"built-in test build.")
    if builtin is not None:
        argv = builtin
        cmd = " ".join(shlex.quote(a) for a in argv)
    else:
        cmd = template.format(device=device)
        script = (shlex.split(cmd) if sys.platform != "win32"
                  else shlex.split(cmd, posix=False) or [""])[0]
        if script.startswith("./") and not os.path.isfile(
                os.path.join(app_dir, script[2:])):
            raise SimError(
                f"no build script at {os.path.join(app_dir, script[2:])}. "
                f"Pass an already-built --prg, or fix CIQKIT_BUILD_CMD.")
        argv = _build_argv(cmd, app_dir)
    # Floor for the freshness check below. Taken before the build starts and
    # nudged back a second, because filesystem mtimes are coarser than
    # time.time() on some filesystems and a fast build can land "before" it.
    started = time.time() - 1.0
    try:
        # UTF-8 with replacement, not the locale: monkeyc prints UTF-8,
        # and decoding it as cp1252 raised UnicodeDecodeError on the first
        # non-ASCII byte -- a successful build reported as a crash.
        # run_killing_tree, not subprocess.run: on a timeout the latter kills
        # only the wrapper and (on Windows) then waits, unbounded, on pipes
        # monkeyc's java still holds -- i.e. for the build to finish anyway.
        r = _compat.run_killing_tree(argv, cwd=app_dir, capture_output=True,
                                     encoding="utf-8", errors="replace",
                                     stdin=subprocess.DEVNULL, timeout=timeout_s,
                                     **hidden_console_kwargs())
    except FileNotFoundError as exc:
        raise SimError(f"build command not executable: {cmd} ({exc})") from exc
    except subprocess.TimeoutExpired as exc:
        raise SimError(f"build timed out after {timeout_s:.0f}s: {cmd}") from exc
    if r.returncode != 0:
        raise SimError(f"build failed: {r.stderr.strip()[-400:] or r.stdout.strip()[-400:]}")

    announced = _announced_prg(r.stdout + "\n" + (r.stderr or ""), app_dir,
                               started)
    if announced:
        return announced

    fresh = _prg_written_since(app_dir, started)
    if fresh:
        return fresh
    raise SimError(
        f"the build command reported success but produced no .prg newer "
        f"than the moment it started, and its output did not name a fresh "
        f"one.\n"
        f"  command: {cmd}\n"
        f"  looked in: {', '.join(BUILD_OUTPUT_DIRS)} under {app_dir}\n"
        f"Refusing to fall back to an existing .prg -- that is how a stale "
        f"or unrelated build (a -t test binary, say) ends up in a "
        f"screenshot. Pass --prg explicitly if your build writes elsewhere.")
