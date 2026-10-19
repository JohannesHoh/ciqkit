"""Cross-process exclusive leases for machine-wide singletons.

The Connect IQ Simulator is one GUI process per machine that can only run one
app at a time, so anything that drives it -- a screenshot run, a test suite, an
interactive session, several coding agents at once -- has to take turns. A
lease makes that explicit:

    from ciqkit import leases
    with leases.lease("sim", timeout=120):
        ...

or from a shell::

    ciqkit lease sim --timeout 120 -- ./scripts/test.sh

Names are free-form; ciqkit itself uses ``sim`` for the simulator and
``build:<project>`` for a project's build directory.

Backed by an exclusive OS file lock on a lock file in a private per-user
directory: ``fcntl.flock`` on POSIX, a ``msvcrt`` byte-range lock on Windows
(see :mod:`ciqkit._compat`). The kernel releases either when the holding
process dies or the fd closes, so there are no stale locks to reap -- the
failure mode that makes mkdir/PID locks unreliable. A best-effort ``.holder``
JSON sidecar records who holds the lease so :func:`status` can show it.

Acquire blocks (polling) until the lease is free or ``timeout`` seconds pass,
at which point it raises :class:`LeaseTimeout`. While waiting it logs the
current holder, so a stuck session is visible rather than a silent hang.
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager

from ._compat import IS_WIN, open_lock_file, try_lock, unlock
from ._compat import pid_alive as _compat_pid_alive

DEFAULT_TIMEOUT = 120.0
_POLL = 0.5


def lease_dir() -> str:
    """Private per-user directory holding the lock files.

    Deliberately not a shared ``/tmp`` path: a lock file another local account
    can create is both a denial of service (they take the lease and never
    release it) and a symlink target. :func:`ciqkit.paths.state_dir` creates it
    0700 and verifies we own it.
    """
    from .paths import state_dir
    return state_dir("leases")

# Leases held by this process tree, propagated to child processes via the
# environment so a lease is REENTRANT across the tree: if an ancestor already
# holds it (a capture run that shells out to build.sh, which itself takes the
# build lease) the inner acquire is a no-op instead of a self-deadlock -- flock
# is not reentrant. This is what lets the lease live at the resource AND at
# call sites without either deadlocking the other.
ENV_KEY = "CIQKIT_LEASES_HELD"
# Lease names contain ':' (build:<app>), so the env list is newline-separated
# (a char that never appears in a lease name).
_ENV_SEP = "\n"


def _held_in_env(name: str) -> bool:
    held = os.environ.get(ENV_KEY, "")
    return name in [h for h in held.split(_ENV_SEP) if h]


class LeaseTimeout(RuntimeError):
    """Raised when a lease could not be acquired within the timeout."""


def _safe_name(name: str) -> str:
    """Map a resource name to a single filesystem-safe filename.

    Names may legitimately contain ':' (``build:my-app``), so only path
    separators and traversal are neutralised. The result is always a bare
    filename -- never a path -- so a crafted name cannot escape the lease
    directory.
    """
    if not name or not name.strip():
        raise ValueError("lease name must be non-empty")
    safe = name.replace("/", "_").replace(os.sep, "_").replace("\\", "_")
    if IS_WIN:
        # ':' is not just invalid on NTFS, it is worse: `build:app.lock` opens
        # the alternate data stream "app.lock" of a file called `build`, so
        # every build lease would share ONE lock file. The holder sidecar keeps
        # the real name, so status() still reports `build:app`.
        safe = "".join("_" if c in '<>:"|?*' or ord(c) < 32 else c for c in safe)
        safe = safe.rstrip(" .")
    safe = safe.strip().lstrip(".")
    if not safe:
        raise ValueError(f"lease name {name!r} reduces to an empty filename")
    return safe


def _paths(name: str):
    base = os.path.join(lease_dir(), _safe_name(name))
    return base + ".lock", base + ".holder"


def _read_holder(holder_path: str):
    try:
        with open(holder_path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    # Never os.kill(pid, 0) here: on Windows it reads a dead holder as alive
    # and sends Ctrl+C to US (see _compat.pid_alive).
    return _compat_pid_alive(pid)


@contextmanager
def lease(name: str, timeout: float = DEFAULT_TIMEOUT, meta: dict | None = None,
          log=None, should_stop=None):
    """Hold an exclusive lease on `name` for the duration of the with-block.

    Blocks until acquired or `timeout` seconds elapse (then raises
    LeaseTimeout). `meta` is merged into the holder sidecar for visibility.
    `log` is an optional callable(str) for waiting messages (defaults to
    stderr). `should_stop` is an optional callable() -> bool polled while
    waiting; when it turns true the wait ends early with LeaseTimeout -- a
    daemon told to shut down must not sit out the rest of a long timeout.
    """
    if log is None:
        def log(msg):
            print(msg, file=sys.stderr, flush=True)

    # Reentrant: an ancestor in this process tree already holds it.
    if _held_in_env(name):
        yield
        return

    lock_path, holder_path = _paths(name)
    fd = open_lock_file(lock_path)
    deadline = time.time() + timeout
    announced = False
    try:
        while not try_lock(fd):
            if should_stop is not None and should_stop():
                raise LeaseTimeout(
                    f"stopped waiting for lease '{name}'") from None
            if time.time() >= deadline:
                holder = _read_holder(holder_path) or {}
                raise LeaseTimeout(
                    "timed out after {:.0f}s waiting for lease '{}' "
                    "(held by {})".format(
                        timeout,
                        name,
                        holder.get("who", "unknown"),
                    )
                ) from None
            if not announced:
                holder = _read_holder(holder_path) or {}
                log("[lease] waiting for '{}' — held by {} since {}".format(
                    name,
                    holder.get("who", "unknown"),
                    holder.get("since", "?"),
                ))
                announced = True
            time.sleep(_POLL)

        holder = {
            # The real name: the file name is a sanitised form of it.
            "resource": name,
            "pid": os.getpid(),
            "who": (meta or {}).get("who")
                   or os.environ.get("CIQKIT_SESSION_LABEL")
                   or f"pid:{os.getpid()}",
            "since": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if meta:
            holder.update(meta)
        try:
            with open(holder_path, "w") as fh:
                json.dump(holder, fh)
        except OSError:
            pass  # sidecar is best-effort visibility, not correctness

        # Publish to the env so child processes see this lease as held and
        # skip re-acquiring it (reentrancy across the process tree).
        prev_env = os.environ.get(ENV_KEY)
        cur = [h for h in (prev_env or "").split(_ENV_SEP) if h]
        cur.append(name)
        os.environ[ENV_KEY] = _ENV_SEP.join(cur)

        try:
            yield
        finally:
            if prev_env is None:
                os.environ.pop(ENV_KEY, None)
            else:
                os.environ[ENV_KEY] = prev_env
            try:
                os.unlink(holder_path)
            except OSError:
                pass
    finally:
        # The lock is released implicitly on close, but be explicit.
        try:
            unlock(fd)
        except OSError:
            pass
        os.close(fd)


def release(name: str) -> tuple:
    """Best-effort manual release of a lease's visibility sidecar.

    A live flock cannot be broken from another process — the kernel only frees
    it when the holder dies. So: if the holder is still alive, refuse and
    return (False, holder) so the caller can report the pid to kill. If the
    holder is dead/absent the lock is ALREADY free; we just clear the stale
    sidecar (cosmetic) and return (True, holder).
    """
    lock_path, holder_path = _paths(name)
    holder = _read_holder(holder_path)
    pid = holder.get("pid") if holder else None
    if pid and _pid_alive(int(pid)):
        return (False, holder)
    try:
        os.unlink(holder_path)
    except OSError:
        pass
    return (True, holder)


def status() -> list:
    """Return a list of currently-held leases (best-effort, from sidecars)."""
    out = []
    d = lease_dir()
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".holder"):
            continue
        name = fn[: -len(".holder")]
        holder = _read_holder(os.path.join(d, fn))
        if not holder:
            continue
        pid = holder.get("pid")
        holder["alive"] = bool(pid) and _pid_alive(int(pid))
        # Prefer the name the holder recorded: the file name is sanitised
        # (on Windows `build:app` is stored as `build_app`).
        holder["resource"] = holder.get("resource") or name
        out.append(holder)
    return out
