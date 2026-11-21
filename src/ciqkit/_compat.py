"""The few OS primitives ciqkit uses that POSIX and Windows spell differently.

Everything here has one contract and two implementations, so the modules that
use it -- leases, the broker's singleton lock, the seed and storage journals --
read the same on both platforms and nobody has to remember which POSIX idiom
quietly does something else on Windows. Stdlib only.
"""
from __future__ import annotations

import errno
import os
import subprocess
import sys

IS_WIN = sys.platform == "win32"

if IS_WIN:
    import msvcrt
else:
    import fcntl

__all__ = ["IS_WIN", "pid_alive", "try_lock", "lock_blocking", "unlock",
           "open_lock_file", "detached_popen", "hidden_console_kwargs",
           "kill_tree", "run_killing_tree", "tree_order"]


def pid_alive(pid) -> bool:
    """True while ``pid`` is a running process. Disturbs nothing.

    Not ``os.kill(pid, 0)`` on Windows, where it is no probe at all: 0 is
    ``signal.CTRL_C_EVENT`` there, so CPython calls GenerateConsoleCtrlEvent.
    Measured on Windows 11 / CPython 3.13: it returns normally for a DEAD pid
    -- so a crashed lease holder or seed owner read as alive forever and was
    never cleaned up -- and for a live one it delivered Ctrl+C to the CALLER's
    own console group, interrupting the caller (the broker's SIGINT handler
    then shut the broker down).
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WIN:
        from . import _winapi
        return _winapi.pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM   # exists, just not ours to signal


def open_lock_file(path: str) -> int:
    """Open (creating 0600) a file whose only job is to be locked."""
    # O_NOFOLLOW: a planted symlink must not make us create or write a file
    # somewhere else under our uid. (Windows has no such flag.)
    flags = (os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    return os.open(path, flags, 0o600)


# Where the Windows byte-range lock sits. Far past any content, because a
# Windows lock is MANDATORY: locking byte 0 would make the pid the broker
# writes into its own lock file unreadable to everyone else. Locking past EOF
# is allowed and does not extend the file.
_WIN_LOCK_OFFSET = 1 << 30


def try_lock(fd: int) -> bool:
    """Take an exclusive lock on ``fd`` without blocking. False if held.

    POSIX: ``flock``. Windows: a one-byte ``msvcrt`` lock. Both are released by
    the kernel when the holder exits or closes the fd, so neither leaves a
    stale lock to reap -- which is the property everything built on this
    relies on.
    """
    if not IS_WIN:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                return False
            raise
    os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return True
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EDEADLK, getattr(errno, "EDEADLOCK", -1)):
            return False
        raise


def lock_blocking(fd: int, poll: float = 0.05) -> None:
    """Exclusive lock, waiting as long as it takes (short critical sections)."""
    if not IS_WIN:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    import time
    while not try_lock(fd):
        time.sleep(poll)


def unlock(fd: int) -> None:
    if not IS_WIN:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def utf8_stdio() -> None:
    """Speak UTF-8 with bare LF on stdin/stdout, as JSON-RPC over stdio needs.

    On Windows a piped stdio defaults to the ANSI code page, so a client's
    UTF-8 request carrying a non-ASCII path (``C:\\Users\\Jörg``) was decoded
    as cp1252 mojibake and named a project that does not exist -- and text
    mode turned every "\\n" we wrote into "\\r\\n". A no-op where stdio
    already is UTF-8/LF.
    """
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", newline="\n")
        except (AttributeError, ValueError):
            pass


# ── child processes ──────────────────────────────────────────────────────────
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_CREATE_SUSPENDED = 0x00000004


def hidden_console_kwargs() -> dict:
    """Popen kwargs that keep a console child from flashing up a window."""
    return {"creationflags": _CREATE_NO_WINDOW} if IS_WIN else {}


def detached_popen(argv, *, gui: bool = False, **kwargs) -> subprocess.Popen:
    """Start a process that outlives its parent and its parent's terminal.

    POSIX: a new session (the portable half of ``setsid``), so a hangup or a
    Ctrl-C in the caller's terminal does not reach it.

    Windows: its own process group (Ctrl-C in the caller's console is not
    delivered to it) and, for console programs, its own HIDDEN console --
    ``CREATE_NO_WINDOW``, not ``DETACHED_PROCESS``. A detached broker has no
    console at all, so every console tool it runs (monkeydo, java, git, a
    build) would pop up a window of its own; with a hidden console they
    inherit that instead. GUI programs (``gui=True``, the simulator) need no
    console and get ``DETACHED_PROCESS``.

    It also asks to break away from the caller's job object: terminals, IDEs
    and agent harnesses run commands inside one that is torn down -- with
    everything in it -- when the command returns, which would take an
    on-demand daemon with it. Not every job permits breakaway, so that is
    retried without.
    """
    if not IS_WIN:
        kwargs.setdefault("start_new_session", True)
        return subprocess.Popen(argv, **kwargs)
    flags = _CREATE_NEW_PROCESS_GROUP | (_DETACHED_PROCESS if gui else _CREATE_NO_WINDOW)
    flags |= kwargs.pop("creationflags", 0)
    try:
        return subprocess.Popen(argv, creationflags=flags | _CREATE_BREAKAWAY_FROM_JOB,
                                **kwargs)
    except PermissionError:
        return subprocess.Popen(argv, creationflags=flags, **kwargs)
    except OSError as exc:
        if getattr(exc, "winerror", None) != 5:   # ERROR_ACCESS_DENIED: no breakaway
            raise
        return subprocess.Popen(argv, creationflags=flags, **kwargs)


def tree_order(procs, roots, born) -> list[int]:
    """Pids of ``roots`` and their descendants, parents before children.

    ``procs`` is ``(pid, parent pid)`` pairs; ``born(pid)`` is a creation time
    or None. A Toolhelp parent link is the creator's pid *at creation* and is
    never updated, so once the creator has exited its pid is reused and the
    link then points at a stranger. A link is therefore followed only when the
    child is known to have been created no earlier than the parent -- a
    process cannot predate its creator. An unknown time (access denied, long
    dead) breaks the link: better to leave a straggler than kill an unrelated
    process. Pure so it runs on every platform.
    """
    kids: dict[int, list[int]] = {}
    for pid, ppid in procs:
        if pid != ppid:   # the idle process names itself as parent
            kids.setdefault(ppid, []).append(pid)
    order: list[int] = []
    found: set[int] = set()
    stack = list(roots)
    while stack:
        pid = stack.pop()
        if pid in found:
            continue
        found.add(pid)
        order.append(pid)
        tp = born(pid)
        for kid in kids.get(pid, ()):
            tk = born(kid)
            if tp is not None and tk is not None and tk >= tp:
                stack.append(kid)
    return order


def _win():
    """The Win32 layer, or None on POSIX. A seam: tests pass a fake instead."""
    if not IS_WIN:
        return None
    from . import _winapi
    return _winapi


def kill_tree(proc: subprocess.Popen, job: int | None = None) -> None:
    """Kill ``proc`` and everything it started. Best effort, never raises.

    POSIX: its process group, which :func:`run_killing_tree` made it the
    leader of (a descendant that calls ``setsid`` itself escapes; monkeyc's
    java and monkeydo's shell do not). Windows: ``job``, the job object
    :func:`run_killing_tree` put it in, else the parent-link tree.
    """
    _kill_tree(proc, job, _win())


def _kill_tree(proc, job, w) -> None:
    try:
        if w is None:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
        elif job is None or not w.terminate_job(job):
            # No job, or it would not terminate: the parent-link walk, which
            # misses a descendant whose parent already exited but is all
            # there is without one.
            w.kill_tree(proc.pid)
    except OSError:
        pass   # already gone (ProcessLookupError) or never ours
    try:
        proc.kill()   # the root at least, whatever the tree walk managed
    except OSError:
        pass


def _popen_in_job(argv, kwargs: dict, w, popen=subprocess.Popen):
    """Windows: start ``argv`` inside a new kill-on-close job. ``(proc, job)``.

    The child is created suspended and resumed only once it is in the job, so
    nothing it spawns can be born outside it -- assigning a running child
    would leave a window in which a fast grandchild escapes. ``job`` is None
    when no job could be made or the child could not be assigned (e.g. an
    enclosing job on a Windows too old to nest them); the child then runs
    anyway and :func:`_kill_tree` falls back to the parent-link walk. A child
    that cannot be resumed is killed, never left frozen until the timeout
    would report it as a hung build.
    """
    job = w.create_kill_job()
    if job is None:
        return popen(argv, **kwargs), None
    flags = kwargs.get("creationflags", 0) | _CREATE_SUSPENDED
    try:
        proc = popen(argv, **{**kwargs, "creationflags": flags})
    except BaseException:
        w.close_handle(job)
        raise
    ok = False
    try:
        if not w.assign_to_job(job, proc.pid):
            w.close_handle(job)
            job = None
        if not w.resume_process(proc.pid):
            raise OSError(f"could not resume {argv!r} after starting it suspended")
        ok = True
    finally:
        if not ok:   # a failed resume, or Ctrl-C between Popen and resume
            _kill_tree(proc, job, w)
            if job is not None:
                w.close_handle(job)
            for f in (proc.stdin, proc.stdout, proc.stderr):
                if f:
                    f.close()   # nothing reads them yet, so this cannot block
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
    return proc, job


def _close_job(job, w, finished: bool) -> None:
    """Close the job handle. A run that ended on its own disarms it first, so
    a background process it left (pipes already closed) is not killed --
    what POSIX does, which never signals the group after a normal exit."""
    if job is None:
        return
    if finished:
        w.disarm_job(job)   # if this fails, the close kills the leftovers; acceptable
    w.close_handle(job)


def run_killing_tree(argv, *, timeout: float, kill_grace_s: float = 2.0,
                     **kwargs) -> subprocess.CompletedProcess:
    """``subprocess.run(argv, timeout=timeout, **kwargs)`` that really returns.

    On a timeout ``subprocess.run`` kills only the direct child -- the bash or
    cmd.exe wrapper -- and on Windows then ``communicate()``s with NO timeout,
    waiting for EOF on pipes that monkeyc's java (or monkeydo's children)
    still hold: the call returned only when the build ended on its own, long
    after the broker had given up on the chunk. On POSIX it returns, but leaves
    that java building in the background, beside the next job's build.

    Here the child leads its own process group / session -- on Windows it also
    runs in its own job object (:func:`_popen_in_job`), because a parent-link
    walk cannot reach a grandchild whose parent already exited, which is what
    a venv's launcher ``python.exe`` or an early-exiting ``cmd /c`` leaves --
    a timeout (or any exception, Ctrl-C included) kills the whole tree, and the output is then
    collected with a bounded ``communicate``. The result and the exception are
    the shapes ``subprocess.run`` gives: a ``CompletedProcess``, or
    ``TimeoutExpired`` carrying whatever output arrived (decoded, when text
    mode was asked for).

    The new group also means a Ctrl-C in the caller's terminal no longer
    reaches the child directly; the caller's KeyboardInterrupt kills the tree
    instead, so the effect is the same.
    """
    if kwargs.pop("capture_output", False):
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    w = _win()
    job = None
    if w is not None:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _CREATE_NEW_PROCESS_GROUP
        proc, job = _popen_in_job(argv, kwargs, w)
    else:
        kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **kwargs)
    finished = False
    try:
        out, err = proc.communicate(timeout=timeout)
        finished = True
    except subprocess.TimeoutExpired as exc:
        _kill_tree(proc, job, w)
        try:
            exc.stdout, exc.stderr = proc.communicate(timeout=kill_grace_s)
        except subprocess.TimeoutExpired:
            # Something outside the tree (a process that escaped the group)
            # still holds a pipe. Give up on the output rather than the
            # deadline. The pipes stay open on Windows: its communicate reads
            # them on threads, and closing a file another thread is reading
            # blocks on that read's lock.
            exc.stdout = exc.stderr = None
            if not IS_WIN:
                for f in (proc.stdout, proc.stderr):
                    if f:
                        f.close()
        raise
    except BaseException:
        _kill_tree(proc, job, w)
        raise
    finally:
        try:
            proc.wait(timeout=kill_grace_s)
        except subprocess.TimeoutExpired:
            pass   # the root ignored SIGKILL/TerminateProcess; nothing more to do
        _close_job(job, w, finished)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
