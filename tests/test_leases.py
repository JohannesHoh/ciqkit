"""Lease semantics: exclusion, reentrancy, timeout, and name sanitising."""
import multiprocessing as mp
import os
import re
import sys
import time

import pytest

from ciqkit import leases


def _own_trustees() -> set:
    """This user's SID and SYSTEM, as an SDDL string names them. SDDL writes
    well-known accounts by alias: the built-in Administrator (RID 500, which
    GitHub's Windows runners run as) appears as "LA", not as its SID."""
    from ciqkit import _winapi
    sid = _winapi.user_sid()
    return {sid, "SY"} | ({"LA"} if sid.endswith("-500") else set())


@pytest.fixture(autouse=True)
def _private_runtime(tmp_path, monkeypatch):
    base = tmp_path / "xdg"
    base.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(base))
    monkeypatch.delenv(leases.ENV_KEY, raising=False)


def test_lease_is_visible_in_status_while_held():
    with leases.lease("sim", timeout=5, meta={"who": "test"}):
        held = {h["resource"]: h for h in leases.status()}
        assert "sim" in held
        assert held["sim"]["who"] == "test"
        assert held["sim"]["alive"] is True
    assert "sim" not in {h["resource"] for h in leases.status()}


def test_lease_is_reentrant_within_a_process_tree():
    """flock is not reentrant, so an inherited env marker makes it so.

    Without this, a lease taken at the resource and again at the call site
    would deadlock against itself.
    """
    with leases.lease("sim", timeout=5):
        with leases.lease("sim", timeout=1):   # must not block
            assert leases.ENV_KEY in os.environ


def _hold(name, ready, release, runtime_dir):
    os.environ["XDG_RUNTIME_DIR"] = runtime_dir
    os.environ.pop(leases.ENV_KEY, None)
    with leases.lease(name, timeout=10, meta={"who": "holder"}):
        ready.set()
        release.wait(timeout=10)


def test_a_second_process_cannot_take_a_held_lease(tmp_path):
    """The exclusion that matters: a different process, not a nested block."""
    ctx = mp.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    runtime = os.environ["XDG_RUNTIME_DIR"]
    p = ctx.Process(target=_hold, args=("sim", ready, release, runtime))
    p.start()
    try:
        assert ready.wait(timeout=15), "holder process never acquired the lease"
        t0 = time.time()
        with pytest.raises(leases.LeaseTimeout):
            with leases.lease("sim", timeout=1.0, log=lambda _m: None):
                pytest.fail("acquired a lease another process holds")
        assert time.time() - t0 >= 0.9, "timeout returned early; it did not wait"
    finally:
        release.set()
        p.join(timeout=15)


def test_release_refuses_while_a_live_process_holds_it(tmp_path):
    ctx = mp.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    runtime = os.environ["XDG_RUNTIME_DIR"]
    p = ctx.Process(target=_hold, args=("sim", ready, release, runtime))
    p.start()
    try:
        assert ready.wait(timeout=15)
        freed, holder = leases.release("sim")
        assert freed is False
        assert holder["pid"] == p.pid
    finally:
        release.set()
        p.join(timeout=15)


@pytest.mark.parametrize("name,expected", [
    ("sim", "sim"),
    # ':' is a legitimate separator -- but on NTFS it opens an alternate data
    # stream, so Windows stores it as '_' (status() still shows the real name).
    ("build:my-app", "build_my-app" if sys.platform == "win32" else "build:my-app"),
    ("a/b", "a_b"),
    ("/", "_"),
    ("../../etc/passwd", "_.._etc_passwd"),  # cannot escape the lease dir
])
def test_names_are_reduced_to_a_bare_filename(name, expected):
    assert leases._safe_name(name) == expected
    assert os.sep not in leases._safe_name(name)


@pytest.mark.parametrize("name", ["", "   ", ".."])
def test_degenerate_names_are_rejected(name):
    with pytest.raises(ValueError):
        leases._safe_name(name)


def test_lock_file_is_not_readable_by_others():
    with leases.lease("sim", timeout=5):
        lock, _holder = leases._paths("sim")
        if sys.platform != "win32":
            assert oct(os.stat(lock).st_mode)[-3:] == "600"
            return
        # Windows has no mode bits: the file inherits the private directory's
        # DACL, so nobody but this user and SYSTEM may open it.
        from ciqkit import _winapi
        sddl = _winapi.dacl_sddl(lock)
        trustees = set(re.findall(r"\([^;]*;[^;]*;[^;]*;[^;]*;[^;]*;([^)]*)\)", sddl))
        assert trustees <= _own_trustees(), sddl


def test_a_stop_request_ends_the_wait_early():
    """A broker told to shut down must not sit out a 180 s lease timeout."""
    ctx = mp.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    runtime = os.environ["XDG_RUNTIME_DIR"]
    p = ctx.Process(target=_hold, args=("sim", ready, release, runtime))
    p.start()
    try:
        assert ready.wait(timeout=15), "holder process never acquired the lease"
        t0 = time.time()
        stop_at = t0 + 0.3
        with pytest.raises(leases.LeaseTimeout, match="stopped"):
            with leases.lease("sim", timeout=30.0, log=lambda _m: None,
                              should_stop=lambda: time.time() >= stop_at):
                pytest.fail("acquired a lease another process holds")
        assert time.time() - t0 < 5, "the wait ignored should_stop"
    finally:
        release.set()
        p.join(timeout=15)

def test_two_build_leases_do_not_share_a_lock_file():
    """On NTFS `build:a.lock` and `build:b.lock` were both alternate data
    streams of ONE file called `build` -- so every project's build lease
    excluded every other's, and status() listed a resource named `build`."""
    with leases.lease("build:app-a", timeout=5):
        with leases.lease("build:app-b", timeout=1, log=lambda _m: None):
            names = {h["resource"] for h in leases.status()}
    assert names == {"build:app-a", "build:app-b"}
    assert leases._paths("build:app-a") != leases._paths("build:app-b")
