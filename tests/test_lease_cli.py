"""`ciqkit lease` -- argument splitting, exit codes, and signal forwarding.

The exit codes are load-bearing: shell callers branch on 75 (EX_TEMPFAIL) to
retry, and on the wrapped command's own code otherwise. Getting either wrong
turns a busy simulator into a spurious build failure.
"""
import multiprocessing as mp
import os
import sys

import pytest

from ciqkit import lease_cli, leases


@pytest.fixture(autouse=True)
def _private_runtime(tmp_path, monkeypatch):
    base = tmp_path / "xdg"
    base.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(base))
    monkeypatch.delenv(leases.ENV_KEY, raising=False)


def test_the_wrapped_commands_exit_code_is_passed_through():
    assert lease_cli.main(["sim", "--", sys.executable, "-c", "raise SystemExit(0)"]) == 0
    assert lease_cli.main(["sim", "--", sys.executable, "-c", "raise SystemExit(7)"]) == 7


def test_the_lease_is_actually_held_while_the_command_runs(tmp_path):
    """Otherwise the wrapper is decoration and two runs still collide."""
    marker = tmp_path / "held.txt"
    pkg_parent = os.path.dirname(os.path.dirname(lease_cli.__file__))
    code = (
        "import json,sys;"
        f"sys.path[:0]=[{pkg_parent!r}];"
        "from ciqkit import leases;"
        f"open({str(marker)!r},'w')"
        ".write(json.dumps([h['resource'] for h in leases.status()]))"
    )
    assert lease_cli.main(["sim", "--who", "test-holder", "--",
                           sys.executable, "-c", code]) == 0
    assert "sim" in marker.read_text()


def test_no_command_is_a_usage_error():
    assert lease_cli.main(["sim"]) == 2


def test_status_lists_nothing_when_idle(capsys):
    assert lease_cli.main(["status"]) == 0
    assert "no leases held" in capsys.readouterr().out


def test_status_shows_a_held_lease(capsys):
    with leases.lease("sim", timeout=5, meta={"who": "someone"}):
        assert lease_cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "sim" in out and "someone" in out


def test_release_of_an_unheld_lease_is_a_no_op(capsys):
    assert lease_cli.main(["release", "sim"]) == 0
    assert "not held" in capsys.readouterr().out


def test_release_without_a_name_is_a_usage_error():
    assert lease_cli.main(["release"]) == 2


def _hold(name, ready, release, runtime_dir):
    os.environ["XDG_RUNTIME_DIR"] = runtime_dir
    os.environ.pop(leases.ENV_KEY, None)
    with leases.lease(name, timeout=10, meta={"who": "holder"}):
        ready.set()
        release.wait(timeout=10)


def test_a_contended_lease_returns_ex_tempfail(capsys):
    """75 is the retry signal; anything else reads as a real failure."""
    ctx = mp.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    p = ctx.Process(target=_hold,
                    args=("sim", ready, release, os.environ["XDG_RUNTIME_DIR"]))
    p.start()
    try:
        assert ready.wait(timeout=15)
        rc = lease_cli.main(["sim", "--timeout", "1", "--",
                             sys.executable, "-c", "raise SystemExit(0)"])
        assert rc == 75
        assert "ciqkit lease" in capsys.readouterr().err
    finally:
        release.set()
        p.join(timeout=15)


def test_release_refuses_while_a_live_process_holds_it(capsys):
    ctx = mp.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    p = ctx.Process(target=_hold,
                    args=("sim", ready, release, os.environ["XDG_RUNTIME_DIR"]))
    p.start()
    try:
        assert ready.wait(timeout=15)
        assert lease_cli.main(["release", "sim"]) == 1
        err = capsys.readouterr().err
        assert "HELD by a LIVE process" in err
        assert str(p.pid) in err       # tell the user what to kill
    finally:
        release.set()
        p.join(timeout=15)


def test_flags_may_appear_before_the_separator_without_eating_the_command():
    """The wrapped command keeps its own flags; only the first `--` splits."""
    rc = lease_cli.main(["sim", "--timeout", "5", "--who", "me", "--",
                         sys.executable, "-c", "import sys; raise SystemExit(len(sys.argv))"])
    assert rc == 1      # argv is just ['-c'] -> length 1
