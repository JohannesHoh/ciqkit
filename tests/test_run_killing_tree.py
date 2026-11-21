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


# ── Windows: the job object, and the walk as its fallback ───────────────────
# The [exit] case above is the one the job fixes. From a venv, sys.executable is
# Scripts\python.exe, a launcher that starts the real interpreter as its own
# child; once that interpreter exits, the Toolhelp parent-link walk cannot link
# the grandchild back to the root, so it survived holding the pipe. A job holds
# every descendant whatever happens to the processes in between.

@pytest.mark.skipif(not _compat.IS_WIN, reason="cmd.exe")
def test_a_grandchild_orphaned_by_an_early_exiting_cmd_is_killed(tmp_path):
    # The same failure without a venv: cmd /c -> base python, which starts the
    # sleeper and exits. /s strips exactly the outer quotes, whatever the path.
    script = tmp_path / "child.py"
    script.write_bytes(_CHILD.encode())
    py = getattr(sys, "_base_executable", sys.executable)
    line = f'cmd /s /c "{subprocess.list2cmdline([py, str(script), "exit"])}"'
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        _compat.run_killing_tree(line, timeout=1, capture_output=True, encoding="utf-8",
                                 errors="replace", stdin=subprocess.DEVNULL,
                                 **_compat.hidden_console_kwargs())
    out = exc.value.stdout
    assert isinstance(out, str) and "GRANDCHILD" in out, out
    gpid = int(out.split("GRANDCHILD", 1)[1].split()[0])
    try:
        assert time.monotonic() - t0 < 8
        assert _wait_dead(gpid), "the grandchild survived the timeout"
    finally:
        if _compat.pid_alive(gpid):
            from ciqkit import _winapi
            _winapi.terminate(gpid)


@pytest.mark.skipif(not _compat.IS_WIN, reason="Win32 job objects")
def test_a_real_job_contains_and_terminates_its_child():
    from ciqkit import _winapi
    proc, job = _compat._popen_in_job(
        [sys.executable, "-c", "import time; time.sleep(60)"], {}, _winapi)
    try:
        assert job is not None, "no job: the struct or a flag was refused"
        assert proc.poll() is None   # resumed, not frozen and not dead
        assert _winapi.terminate_job(job)
        proc.wait(timeout=5)
    finally:
        proc.kill()
        _compat._close_job(job, _winapi, finished=False)


class _FakeProc:
    def __init__(self, argv, **kwargs):
        self.argv, self.kwargs = argv, kwargs
        self.pid = 4242
        self.stdin = self.stdout = self.stderr = None
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 1


class _FakeWin:
    """The _winapi surface _compat uses, recording the calls in order."""

    def __init__(self, job=7, assign=True, resume=True, terminate=True):
        self.job, self.assign, self.resume, self.terminate = job, assign, resume, terminate
        self.calls = []

    def create_kill_job(self):
        self.calls.append("create")
        return self.job

    def assign_to_job(self, job, pid):
        self.calls.append(("assign", job, pid))
        return self.assign

    def resume_process(self, pid):
        self.calls.append(("resume", pid))
        return self.resume

    def terminate_job(self, job):
        self.calls.append(("terminate_job", job))
        return self.terminate

    def kill_tree(self, pid):
        self.calls.append(("walk", pid))

    def disarm_job(self, job):
        self.calls.append(("disarm", job))
        return True

    def close_handle(self, h):
        self.calls.append(("close", h))


def _start(w, **kwargs):
    made = []

    def popen(argv, **kw):
        made.append(_FakeProc(argv, **kw))
        return made[-1]
    proc, job = _compat._popen_in_job(["build"], kwargs, w, popen=popen)
    return proc, job


def test_the_child_starts_suspended_and_resumes_only_once_in_the_job():
    w = _FakeWin()
    proc, job = _start(w, creationflags=0x200)
    assert job == 7
    assert proc.kwargs["creationflags"] == 0x200 | _compat._CREATE_SUSPENDED
    assert w.calls == ["create", ("assign", 7, 4242), ("resume", 4242)]


def test_no_job_means_a_plain_start_and_the_walk_on_timeout():
    w = _FakeWin(job=None)
    proc, job = _start(w, creationflags=0x200)
    assert job is None and proc.kwargs["creationflags"] == 0x200   # not suspended
    _compat._kill_tree(proc, job, w)
    assert w.calls[-1] == ("walk", 4242) and proc.killed


def test_a_refused_assignment_still_resumes_and_falls_back():
    w = _FakeWin(assign=False)
    proc, job = _start(w)
    assert job is None
    assert w.calls == ["create", ("assign", 7, 4242), ("close", 7), ("resume", 4242)]


def test_a_child_that_cannot_be_resumed_is_killed_not_left_frozen():
    w = _FakeWin(resume=False)
    with pytest.raises(OSError, match="resume"):
        _start(w)
    assert ("terminate_job", 7) in w.calls and ("close", 7) in w.calls


def test_a_failed_popen_closes_the_job():
    w = _FakeWin()

    def popen(argv, **kw):
        raise FileNotFoundError(argv[0])
    with pytest.raises(FileNotFoundError):
        _compat._popen_in_job(["nope"], {}, w, popen=popen)
    assert w.calls == ["create", ("close", 7)]


def test_a_timeout_terminates_the_job_and_skips_the_walk():
    w = _FakeWin()
    proc = _FakeProc(["build"])
    _compat._kill_tree(proc, 7, w)
    assert w.calls == [("terminate_job", 7)] and proc.killed


def test_a_job_that_will_not_terminate_falls_back_to_the_walk():
    w = _FakeWin(terminate=False)
    _compat._kill_tree(_FakeProc(["build"]), 7, w)
    assert w.calls == [("terminate_job", 7), ("walk", 4242)]


def test_a_finished_run_disarms_the_job_before_closing_it():
    # Kill-on-close would otherwise take a deliberate background process with it.
    w = _FakeWin()
    _compat._close_job(7, w, finished=True)
    assert w.calls == [("disarm", 7), ("close", 7)]
    w.calls.clear()
    _compat._close_job(7, w, finished=False)
    assert w.calls == [("close", 7)]
    _compat._close_job(None, w, finished=True)
    assert w.calls == [("close", 7)]
