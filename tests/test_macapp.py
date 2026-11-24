"""ciqkit.app: the broker under its own name (macOS only, opt-in)."""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys

import pytest

from ciqkit import macapp

mac = pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("clang"),
                         reason="needs macOS and clang")


@pytest.fixture
def app(monkeypatch, tmp_path):
    """The real launcher, built around a stand-in interpreter that reports how it
    was called. No LaunchServices: the launcher is run directly."""
    out = tmp_path / "out"
    py = tmp_path / "py thon"
    py.write_text('#!/bin/sh\n{ echo "args: $*"; echo "cwd: $(pwd -P)"; env; } '
                  f'> "{out}"\necho to-the-log\nexit 7\n')
    py.chmod(0o755)
    real = macapp._run

    def run(argv, **kw):
        if argv[0] == str(py):
            return subprocess.CompletedProcess(argv, 0, macapp.__file__ + "\n", "")
        return real(argv, **kw)
    monkeypatch.setattr(macapp, "_run", run)
    path, rebuilt = macapp.install(str(py))
    assert rebuilt
    return path, str(py), out


def _launch(path, envfile):
    return subprocess.run([os.path.join(path, "Contents/MacOS/ciqkit"), str(envfile)],
                          capture_output=True, text=True, timeout=30)


def _envfile(tmp_path, *pairs, mode=0o600):
    f = tmp_path / "env"
    f.write_bytes(b"".join(p.encode() + b"\0" for p in pairs))
    f.chmod(mode)
    return f


@mac
def test_launcher_runs_only_the_broker_and_drops_injection_variables(app, tmp_path):
    path, _py, out = app
    log = tmp_path / "log"
    f = _envfile(tmp_path, "PATH=/usr/bin:/bin", f"CIQKIT_APP_LOG={log}",
                 f"CIQKIT_APP_CWD={tmp_path}", "DYLD_INSERT_LIBRARIES=/evil.dylib",
                 "PYTHONPATH=/evil", "PYTHONHOME=/evil", "KEEP=a=b c")
    assert _launch(path, f).returncode == 7          # the broker's own status
    text = out.read_text()
    assert "args: -I -m ciqkit.sim.broker\n" in text
    assert f"cwd: {os.path.realpath(tmp_path)}\n" in text
    assert "KEEP=a=b c\n" in text
    assert "evil" not in text
    assert "to-the-log" in log.read_text()
    assert not f.exists()                            # the environment is not left lying about


@mac
@pytest.mark.parametrize("mode", [0o644, 0o660])
def test_launcher_refuses_an_environment_others_could_write_or_read(app, tmp_path, mode):
    path, _py, out = app
    assert _launch(path, _envfile(tmp_path, "A=1", mode=mode)).returncode == 77
    assert not out.exists()


@mac
def test_launcher_refuses_a_symlink_and_a_missing_argument(app, tmp_path):
    path, _py, out = app
    link = tmp_path / "link"
    link.symlink_to(_envfile(tmp_path, "A=1"))
    assert _launch(path, link).returncode == 66
    exe = os.path.join(path, "Contents/MacOS/ciqkit")
    assert subprocess.run([exe]).returncode == 64
    assert not out.exists()


@mac
def test_installing_again_keeps_the_signature_macos_granted(app):
    path, py, _out = app
    exe = os.path.join(path, "Contents/MacOS/ciqkit")
    before = os.stat(exe).st_ino
    assert macapp.install(py) == (path, False)
    assert os.stat(exe).st_ino == before
    assert macapp.install(py, force=True) == (path, True)


@mac
def test_an_app_for_another_checkout_is_not_used(app):
    path, _py, _out = app
    assert macapp.installed_for_me()
    plist = os.path.join(path, "Contents/Info.plist")
    with open(plist, "rb") as f:
        info = plistlib.load(f)
    info["CIQKitInstall"] = "/somewhere/else"
    with open(plist, "wb") as f:
        plistlib.dump(info, f)
    assert not macapp.installed_for_me()


@mac
def test_a_foreign_app_is_never_replaced_or_removed(monkeypatch):
    os.makedirs(os.path.join(macapp.app_path(), "Contents"))
    with pytest.raises(macapp.AppError, match="not ciqkit's"):
        macapp.install("/usr/bin/true")
    assert macapp.uninstall() is False
    assert os.path.isdir(macapp.app_path())


def test_off_macos_there_is_no_app(monkeypatch):
    monkeypatch.setattr(macapp.sys, "platform", "linux")
    assert macapp.info() is None and not macapp.installed_for_me()
    with pytest.raises(macapp.AppError):
        macapp.install("/usr/bin/true")


@pytest.fixture
def spawn(monkeypatch, tmp_path):
    from ciqkit.sim import client
    monkeypatch.delenv("CIQKIT_PYTHON", raising=False)
    monkeypatch.setattr(client.proto, "runtime_dir", lambda: str(tmp_path))
    monkeypatch.setattr(client, "resolve_broker_python", lambda: "/usr/bin/works")
    seen = []
    monkeypatch.setattr(client, "detached_popen", lambda argv, **kw: seen.append(argv))
    monkeypatch.setattr(macapp, "installed_for_me", lambda: True)
    return lambda: (client.spawn_daemon(str(tmp_path / "sock")), seen)[1]


def test_the_broker_starts_through_the_app(spawn, monkeypatch, tmp_path):
    got = []
    monkeypatch.setattr(macapp, "launch", lambda env, log: got.append(env))
    assert spawn() == []
    assert got[0]["CIQSIM_SOCK"] == str(tmp_path / "sock")


def test_a_failing_app_falls_back_and_says_so(spawn, monkeypatch, tmp_path):
    def boom(env, log):
        raise macapp.AppError("no")
    monkeypatch.setattr(macapp, "launch", boom)
    assert spawn() == [["/usr/bin/works", "-m", "ciqkit.sim.broker"]]
    assert "did not start (no)" in (tmp_path / "brokerd.log").read_text()


def test_an_explicit_interpreter_beats_the_app(spawn, monkeypatch):
    monkeypatch.setenv("CIQKIT_PYTHON", "/usr/bin/mine")
    monkeypatch.setattr(macapp, "launch", lambda *a: pytest.fail("used the app"))
    assert len(spawn()) == 1


def test_a_refused_launch_leaves_no_environment_behind(monkeypatch, tmp_path):
    monkeypatch.setattr(macapp.paths, "runtime_dir", lambda: str(tmp_path))
    monkeypatch.setattr(macapp, "_run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "nope"))
    with pytest.raises(macapp.AppError, match="nope"):
        macapp.launch({"SECRET": "x"}, str(tmp_path / "log"))
    assert list(tmp_path.iterdir()) == []


def test_preflight_op_reports_the_brokers_own_verdict(monkeypatch):
    from ciqkit.sim import preflight
    monkeypatch.setattr(preflight.sys, "platform", "linux")
    monkeypatch.setenv("CIQKIT_APP", "1")

    def fail():
        raise preflight.PreflightError("no permission")
    monkeypatch.setattr(preflight, "check_once", fail)
    rep = preflight.report()
    assert rep == {"ok": True, "as_app": True, "passed": False, "error": "no permission"}


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS")
def test_the_permission_probe_really_answers(monkeypatch):
    """The child runs with -I, so a wrong module name only showed as missing keys."""
    from ciqkit.sim import preflight
    src = os.path.dirname(os.path.dirname(macapp.__file__))
    real = subprocess.run

    def run(argv, **kw):
        assert argv[1:3] == ["-I", "-m"]
        return real([argv[0], *argv[2:]], env=dict(os.environ, PYTHONPATH=src), **kw)
    monkeypatch.setattr(preflight.subprocess, "run", run)
    monkeypatch.setattr(preflight, "check_once", lambda: {})
    rep = preflight.report()
    assert isinstance(rep["accessibility"], bool) and isinstance(rep["screen_recording"], bool)
