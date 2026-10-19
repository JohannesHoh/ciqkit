"""Every declared console script must resolve to something importable.

Declared-but-missing is a quiet failure: `pyproject.toml` gains an entry point,
the editable install in a long-lived venv predates it, and the binary the docs
tell people to run is simply not there. That happened to a console script the
day it was added, and to a stats server before it.

This suite cannot detect a stale install (a fresh CI venv always has the
binaries, and a developer's venv is their own business). What it CAN pin is the
half that is a code defect: a declared target that does not import, or names an
attribute that is not there — the shape a rename leaves behind.
"""
import importlib
import pathlib
import re
import sys

import pytest

from ciqkit import cli

PYPROJECT = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"


def _declared_scripts() -> dict[str, str]:
    text = PYPROJECT.read_text()
    section = text.split("[project.scripts]", 1)[1].split("\n[", 1)[0]
    return dict(re.findall(r'^\s*([\w-]+)\s*=\s*"([^"]+)"\s*$', section, re.M))


def test_there_are_scripts_to_check():
    scripts = _declared_scripts()
    assert scripts, "no [project.scripts] parsed — the regex or the file moved"
    assert "ciqkit" in scripts


@pytest.mark.parametrize("name,target", sorted(_declared_scripts().items()))
def test_each_console_script_target_exists(name, target):
    module_path, _, attr = target.partition(":")
    module = importlib.import_module(module_path)
    fn = getattr(module, attr, None)
    assert callable(fn), f"{name} -> {target} is not callable"


def test_the_simulator_server_is_declared():
    assert _declared_scripts().get("ciqkit-mcp") == "ciqkit.sim.mcp_server:main"


# --- doctor's install check -------------------------------------------------
# `ciqkit doctor` is where a stale install is catchable at all: the suite above
# runs in a venv that by construction has the right scripts. These tests drive
# the check with a fabricated interpreter location, so each failure mode is
# exercised without touching a real environment.

def _exe(name):
    # What pip writes: `ciqkit.exe` on Windows, `ciqkit` elsewhere.
    return name + (".exe" if sys.platform == "win32" else "")


def _fake_interpreter(tmp_path, monkeypatch, scripts=()):
    binp = tmp_path / "bin"
    binp.mkdir(exist_ok=True)
    for name in scripts:
        (binp / _exe(name)).write_text("#!/bin/sh\n")
    monkeypatch.setattr(cli.sys, "executable", str(binp / "python"))
    return binp


def test_a_missing_script_is_reported_with_the_fix(tmp_path, monkeypatch):
    _fake_interpreter(tmp_path, monkeypatch)          # no scripts at all
    ok, lines = cli._check_console_scripts()
    assert ok is False
    assert any("NOT INSTALLED" in ln for ln in lines)
    assert any("pip install -e ." in ln for ln in lines)


def test_a_present_script_passes(tmp_path, monkeypatch):
    names = [n for n in _declared_scripts()]
    binp = _fake_interpreter(tmp_path, monkeypatch, scripts=names)
    # Only the fake interpreter's bin on PATH: the real venv running pytest is
    # on PATH in CI and would "shadow" every fake script.
    monkeypatch.setenv("PATH", str(binp))
    ok, lines = cli._check_console_scripts()
    assert ok is True, lines
    assert all("ok" in ln for ln in lines)


def test_an_unactivated_venv_is_not_a_failure(tmp_path, monkeypatch):
    """No script on PATH is normal — it means the venv is not activated.

    Treating that as broken would cry wolf on every correct install driven by
    absolute path, which is how an MCP client launches these.
    """
    names = [n for n in _declared_scripts()]
    _fake_interpreter(tmp_path, monkeypatch, scripts=names)
    import shutil as _sh
    monkeypatch.setattr(_sh, "which", lambda name: None)
    ok, lines = cli._check_console_scripts()
    assert ok is True
    assert any("not on PATH" in ln for ln in lines)


def test_a_shadowing_copy_on_path_is_a_failure(tmp_path, monkeypatch):
    """Two venvs here can hold ciqkit editable from one source.

    If PATH resolves the name to the other one, the user is running code this
    interpreter is not — invisible to both an `ls` and the test suite.
    """
    names = [n for n in _declared_scripts()]
    _fake_interpreter(tmp_path, monkeypatch, scripts=names)
    other = tmp_path / "other-venv" / "bin"
    other.mkdir(parents=True)
    for n in names:
        (other / _exe(n)).write_text("#!/bin/sh\n")
    import shutil as _sh
    monkeypatch.setattr(_sh, "which", lambda name: str(other / _exe(name)))
    ok, lines = cli._check_console_scripts()
    assert ok is False
    assert any("SHADOWED" in ln for ln in lines)


def test_running_from_source_is_not_a_failure(monkeypatch):
    import importlib.metadata as md

    def boom(_name):
        raise md.PackageNotFoundError("ciqkit")

    monkeypatch.setattr(md, "distribution", boom)
    ok, lines = cli._check_console_scripts()
    assert ok is True
    assert any("not installed as a distribution" in ln for ln in lines)
