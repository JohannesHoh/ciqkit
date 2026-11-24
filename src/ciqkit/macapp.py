"""``ciqkit.app``: the broker's own name in macOS' privacy settings.

macOS grants Accessibility and Screen Recording to the *responsible* process,
which for a command-line tool is whatever started it: a terminal, an editor, or
(for a tool run by a launchd service) ``node`` or ``Python``. The grant then
covers every script that program runs, and it goes missing whenever the program
is replaced or the session is started some other way.

A process that LaunchServices starts is responsible for itself, and its children
inherit that. So ``ciqkit mac-app install`` builds a tiny background app whose
only job is to run this install's broker, and the client starts the broker
through it. Opt-in; without the app nothing changes.

What it does not do: isolate. The launcher runs one fixed interpreter and module
(never a program named on its command line), isolated from PYTHON*/DYLD_*
variables, but ciqkit's own Python files are yours to edit, so anything that
runs as you can still reach the permission by changing them.
"""
from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile

from . import paths

__all__ = ["BUNDLE_ID", "AppError", "app_path", "info", "install", "installed_for_me",
           "launch", "uninstall"]

BUNDLE_ID = "io.github.johanneshoh.ciqkit"

# The launcher must NOT exec the interpreter: it has to stay alive as the
# responsible process, with python as an ordinary child.
_SOURCE = r"""
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <spawn.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#define MAX_ENV (1 << 20)
#define MAX_VARS 8192
static volatile sig_atomic_t child = 0;
static void forward(int sig) { if (child > 0) kill((pid_t)child, sig); }

static const char *value(const char *var, const char *name) {
  size_t n = strlen(name);
  return strncmp(var, name, n) == 0 && var[n] == '=' ? var + n + 1 : 0;
}

int main(int argc, char **argv) {
  if (argc != 2) return 64;
  int fd = open(argv[1], O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
  struct stat st;
  if (fd < 0) return 66;
  if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_uid != getuid() ||
      (st.st_mode & 077) || st.st_size > MAX_ENV) return 77;
  char *buf = calloc(1, (size_t)st.st_size + 1);
  if (!buf) return 71;
  ssize_t got = 0, r;
  while (got < st.st_size && (r = read(fd, buf + got, (size_t)(st.st_size - got))) > 0) got += r;
  close(fd);
  unlink(argv[1]);
  if (got != st.st_size) return 74;

  static char *env[MAX_VARS + 1];
  int n = 0;
  for (char *p = buf; p < buf + got; p += strlen(p) + 1) {
    if (!*p || !strncmp(p, "DYLD_", 5) || !strncmp(p, "PYTHON", 6)) continue;
    const char *v;
    if ((v = value(p, "CIQKIT_APP_LOG"))) {
      int log = open(v, O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW, 0600);
      if (log >= 0) { dup2(log, 1); dup2(log, 2); if (log > 2) close(log); }
    } else if ((v = value(p, "CIQKIT_APP_CWD"))) {
      if (chdir(v)) { /* gone: keep / */ }
    }
    if (n < MAX_VARS) env[n++] = p;
  }
  int null = open("/dev/null", O_RDONLY);
  if (null > 0) { dup2(null, 0); close(null); }

  char *args[] = {CIQKIT_PY, "-I", "-m", "ciqkit.sim.broker", 0};
  struct sigaction sa;
  memset(&sa, 0, sizeof sa);
  sa.sa_handler = forward;
  sigaction(SIGTERM, &sa, 0); sigaction(SIGINT, &sa, 0); sigaction(SIGHUP, &sa, 0);
  pid_t pid;
  if (posix_spawn(&pid, CIQKIT_PY, 0, 0, args, env)) return 71;
  child = pid;
  int status;
  while (waitpid(pid, &status, 0) < 0) if (errno != EINTR) return 71;
  return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
}
"""

_PROBE = "import PIL, numpy, pyautogui, ciqkit, ciqkit.sim.broker; print(ciqkit.__file__)"


class AppError(RuntimeError):
    """ciqkit.app could not be built, or is not usable."""


def app_path() -> str:
    base = os.environ.get("CIQKIT_APP_DIR") or os.path.expanduser("~/Applications")
    return os.path.join(base, "ciqkit.app")


def _install_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def info() -> dict | None:
    """The installed app's Info.plist, if there is one and it is ours."""
    if sys.platform != "darwin":
        return None
    try:
        with open(os.path.join(app_path(), "Contents", "Info.plist"), "rb") as f:
            plist = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return plist if plist.get("CFBundleIdentifier") == BUNDLE_ID else None


def installed_for_me() -> bool:
    """Is there an app, built to run the same ciqkit that is asking?

    Two checkouts each have their own broker code; the app must never start the
    other one's behind the caller's back.
    """
    plist = info()
    return bool(plist and plist.get("CIQKitInstall") == _install_dir())


def _run(argv, **kw):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=120, **kw)
    except FileNotFoundError as exc:
        raise AppError(f"{argv[0]} is missing; install Apple's command line tools "
                       f"(xcode-select --install)") from exc
    except subprocess.TimeoutExpired as exc:
        raise AppError(f"{argv[0]} did not finish within 120 s") from exc


def _digest(python: str) -> str:
    return hashlib.sha256("\0".join((_SOURCE, python, _install_dir())).encode()).hexdigest()


def install(python: str, *, force: bool = False) -> tuple[str, bool]:
    """Build the app for ``python``. Returns (path, rebuilt).

    Rebuilding changes the code signature and macOS then drops the grant, so an
    app that is already right is left alone.
    """
    if sys.platform != "darwin":
        raise AppError("ciqkit.app is a macOS thing; nothing to do here")
    path = app_path()
    digest = _digest(python)
    current = info()
    if current and current.get("CIQKitDigest") == digest and not force:
        return path, False
    if os.path.lexists(path) and current is None:
        raise AppError(f"{path} exists and is not ciqkit's; move it away first")
    # -I is how the launcher runs it, so ciqkit has to be installed there
    # (an editable install counts), not merely on PYTHONPATH.
    probe = _run([python, "-I", "-c", _PROBE])
    if probe.returncode != 0:
        raise AppError(f"{python} cannot run the broker on its own:\n"
                       f"  {(probe.stderr or '').strip().splitlines()[-1:]}\n"
                       f"  install ciqkit[sim] into it, or pass --python")
    theirs = os.path.dirname(os.path.abspath(probe.stdout.strip()))
    if theirs != _install_dir():
        raise AppError(f"{python} would run ciqkit from {theirs}, not {_install_dir()}")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".ciqkit-app-", dir=os.path.dirname(path))
    try:
        staged = os.path.join(tmp, "ciqkit.app")
        macos = os.path.join(staged, "Contents", "MacOS")
        os.makedirs(macos)
        src = os.path.join(tmp, "launcher.c")
        with open(src, "w", encoding="utf-8") as f:
            quoted = python.replace("\\", "\\\\").replace('"', '\\"')
            f.write(f'#define CIQKIT_PY "{quoted}"\n{_SOURCE}')
        built = _run(["clang", "-O2", "-Wall", "-o", os.path.join(macos, "ciqkit"), src])
        if built.returncode != 0:
            raise AppError(f"the launcher did not compile:\n{built.stderr}")
        with open(os.path.join(staged, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({
                "CFBundleIdentifier": BUNDLE_ID, "CFBundleName": "ciqkit",
                "CFBundleDisplayName": "ciqkit", "CFBundleExecutable": "ciqkit",
                "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1",
                "CFBundleVersion": "1", "LSUIElement": True,
                "NSAppleEventsUsageDescription":
                    "ciqkit positions and drives the Connect IQ simulator window.",
                "CIQKitPython": python, "CIQKitInstall": _install_dir(),
                "CIQKitDigest": digest}, f)
        signed = _run(["codesign", "--sign", "-", "--force", "--identifier", BUNDLE_ID, staged])
        if signed.returncode != 0:
            raise AppError(f"codesign failed:\n{signed.stderr}")
        if os.path.lexists(path):
            os.rename(path, os.path.join(tmp, "old.app"))
        os.rename(staged, path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return path, True


def uninstall() -> bool:
    if info() is None:
        return False
    shutil.rmtree(app_path())
    return True


def launch(env: dict, log_path: str) -> None:
    """Start the broker through the app. Raises AppError if macOS refuses.

    The environment travels in a private file the launcher deletes, not in
    ``open --env``: arguments are readable by every process on the machine.
    """
    env = dict(env, CIQKIT_APP_LOG=log_path, CIQKIT_APP_CWD=os.getcwd(), CIQKIT_APP="1")
    fd, path = tempfile.mkstemp(prefix="app-env-", dir=paths.runtime_dir())
    try:
        with os.fdopen(fd, "wb") as f:
            for k, v in env.items():
                f.write(os.fsencode(f"{k}={v}") + b"\0")
        r = _run(["/usr/bin/open", "-g", "-n", app_path(), "--args", path])
        if r.returncode != 0:
            raise AppError((r.stderr or "open failed").strip())
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
