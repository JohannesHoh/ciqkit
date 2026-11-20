"""_compat.run_killing_tree: a timed-out build or test run must return on time
and take its whole process tree with it.

subprocess.run kills only the direct child (bash, cmd.exe) on a timeout; on
Windows it then waited, unbounded, on pipes the grandchild (monkeyc's java)
still held. These run a real grandchild that holds the pipe.
"""
import subprocess
import sys
import time

import pytest

from ciqkit import _compat

# The child starts a grandchild that inherits stdout explicitly (so it holds
# the pipe on Windows too), reports its pid, then either waits like a bash
# wrapper does or exits and leaves it orphaned.
_CHILD = """
import subprocess, sys, time
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                     stdout=sys.stdout, stderr=sys.stderr)
print("GRANDCHILD", g.pid, flush=True)
if sys.argv[1] == "wait":
    time.sleep(60)
"""


def _wait_dead(pid, within=5.0):
    # A SIGKILLed orphan is a zombie until init reaps it; give that a moment.
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _compat.pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("child", ["wait", "exit"])
def test_a_timeout_returns_promptly_and_kills_the_grandchild(child):
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        _compat.run_killing_tree([sys.executable, "-c", _CHILD, child], timeout=1,
                                 capture_output=True, encoding="utf-8",
                                 errors="replace", stdin=subprocess.DEVNULL,
                                 **_compat.hidden_console_kwargs())
    elapsed = time.monotonic() - t0
    out = exc.value.stdout
    assert isinstance(out, str) and "GRANDCHILD" in out, out
    gpid = int(out.split("GRANDCHILD", 1)[1].split()[0])
    try:
        assert elapsed < 8, f"took {elapsed:.1f}s: the pipe holder kept it waiting"
        assert _wait_dead(gpid), "the grandchild survived the timeout"
    finally:
        if _compat.pid_alive(gpid):   # never leave a 60 s sleeper behind
            if _compat.IS_WIN:
                from ciqkit import _winapi
                _winapi.terminate(gpid)
            else:
                import os
                import signal
                os.kill(gpid, signal.SIGKILL)


def test_a_run_inside_its_timeout_is_a_completed_process():
    r = _compat.run_killing_tree(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); "
                               "sys.exit(3)"],
        timeout=30, capture_output=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL)
    assert isinstance(r, subprocess.CompletedProcess)
    assert r.returncode == 3 and r.stdout.strip() == "out" and r.stderr.strip() == "err"
