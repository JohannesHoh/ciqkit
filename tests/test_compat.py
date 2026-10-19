"""The OS primitives that differ between POSIX and Windows (ciqkit._compat).

Each of these had a POSIX idiom that does something else -- or nothing -- on
Windows, so each gets pinned on whatever platform the suite runs on.
"""
import os
import subprocess
import sys
import time

import pytest

from ciqkit import _compat

IS_WIN = sys.platform == "win32"


def _sleeper(seconds=30):
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def test_asking_whether_a_process_is_alive_does_not_kill_it():
    """`os.kill(pid, 0)` is the POSIX liveness probe. On Windows 0 is
    CTRL_C_EVENT: it Ctrl+C'd the CALLER (the broker shut itself down when it
    checked on a lease holder). The probe must be a probe everywhere."""
    p = _sleeper()
    try:
        assert _compat.pid_alive(p.pid) is True
        time.sleep(0.3)
        assert p.poll() is None, "the liveness probe terminated the process"
        assert _compat.pid_alive(p.pid) is True
    finally:
        p.kill()
        p.wait()


def test_a_finished_process_is_not_alive():
    """On Windows os.kill(dead_pid, 0) returns normally, so a crashed lease
    holder or seed owner read as alive forever and was never cleaned up."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    # A pid can be recycled, but not within this window on any sane system.
    assert _compat.pid_alive(p.pid) is False


@pytest.mark.parametrize("pid", [0, -1, None, "x"])
def test_nonsense_pids_are_not_alive(pid):
    assert _compat.pid_alive(pid) is False


def test_the_lock_excludes_a_second_holder_and_frees_on_close(tmp_path):
    path = str(tmp_path / "x.lock")
    a = _compat.open_lock_file(path)
    b = _compat.open_lock_file(path)
    try:
        assert _compat.try_lock(a) is True
        assert _compat.try_lock(b) is False
        _compat.unlock(a)
        assert _compat.try_lock(b) is True
    finally:
        os.close(a)
        os.close(b)


def test_the_windows_lock_leaves_the_file_readable(tmp_path):
    """Windows byte-range locks are mandatory. The broker writes its pid into
    its singleton lock file; locking byte 0 would make that unreadable to
    anyone diagnosing a stuck daemon. The lock sits far past the content."""
    path = str(tmp_path / "pid.lock")
    fd = _compat.open_lock_file(path)
    try:
        assert _compat.try_lock(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, b"1234\n")
        with open(path, "rb") as fh:
            assert fh.read() == b"1234\n"
    finally:
        os.close(fd)


def test_a_detached_child_runs_and_reports_back(tmp_path):
    out = tmp_path / "out.txt"
    p = _compat.detached_popen(
        [sys.executable, "-c", f"open({str(out)!r}, 'w').write('ran')"],
        stdin=subprocess.DEVNULL)
    assert p.wait(timeout=30) == 0
    assert out.read_text() == "ran"


@pytest.mark.skipif(not IS_WIN, reason="Windows locked-session handling")
def test_gestures_refuse_to_fire_into_a_locked_session(monkeypatch):
    """Locked, Windows discards synthesized input. Firing anyway turned a
    one-line cause into three silent retries and a minute-long "the screen
    did not change"."""
    from ciqkit import _winapi
    from ciqkit.capture import screenshot_utils as su
    monkeypatch.setattr(_winapi, "session_locked", lambda: True)
    win = su._WinWindow(0, 0, 0, 0, 100, 100)
    with pytest.raises(su.SimulatorInputError, match="locked"):
        su._win_focus(win)


@pytest.mark.skipif(not IS_WIN, reason="Win32 process and port tables")
def test_this_process_and_its_listening_port_are_found():
    """Read from the kernel tables, not from `tasklist`/`netstat`, whose
    output is localised (a German box says ABHÖREN, not LISTENING)."""
    import socket
    from ciqkit import _winapi
    me = os.getpid()
    assert any(pid == me for pid, _pp, _n in _winapi.processes())
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    try:
        port = s.getsockname()[1]
        assert (port, me, "127.0.0.1") in _winapi.tcp_listeners()
    finally:
        s.close()
    assert _winapi.command_line(me) and "python" in _winapi.command_line(me).lower()
