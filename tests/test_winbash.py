"""winbash: find Git for Windows' bash, never the WSL launcher on PATH.

Everything else mocks ``git_bash`` away, so the search itself is pinned here
with fake install trees; nothing needs Windows or a real Git.
"""
import os
import subprocess

import pytest

from ciqkit import winbash


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(winbash, "_cache", {})
    for env in ("CIQKIT_BASH", "ProgramW6432", "ProgramFiles", "ProgramFiles(x86)",
                "LOCALAPPDATA"):
        monkeypatch.delenv(env, raising=False)


def _touch(*parts):
    path = os.path.join(*parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb"):
        pass
    return path


def _which(mapping, asked):
    def which(name):
        asked.append(name)
        return mapping.get(name)
    return which


def test_override_wins(monkeypatch):
    monkeypatch.setenv("CIQKIT_BASH", r"D:\tools\bash.exe")
    assert winbash.git_bash() == r"D:\tools\bash.exe"


@pytest.mark.parametrize("git_rel", [("cmd", "git.exe"), ("bin", "git.exe"),
                                     ("mingw64", "bin", "git.exe")])
def test_takes_the_bash_beside_git(tmp_path, monkeypatch, git_rel):
    root = os.path.join(os.path.realpath(tmp_path), "Git")
    git = _touch(root, *git_rel)
    bash = _touch(root, "bin", "bash.exe")
    monkeypatch.setattr(winbash.shutil, "which", _which({"git": git}, []))
    assert winbash.git_bash() == bash


def test_falls_back_to_usr_bin_bash(tmp_path, monkeypatch):
    root = os.path.join(os.path.realpath(tmp_path), "Git")
    git = _touch(root, "cmd", "git.exe")
    bash = _touch(root, "usr", "bin", "bash.exe")
    monkeypatch.setattr(winbash.shutil, "which", _which({"git": git}, []))
    assert winbash.git_bash() == bash


def test_never_asks_path_for_bash(tmp_path, monkeypatch):
    # The first bash.exe on a stock Windows 11 PATH is WSL's; it must not leak in.
    wsl = _touch(str(tmp_path), "System32", "bash.exe")
    asked = []
    monkeypatch.setattr(winbash.shutil, "which", _which({"bash": wsl}, asked))
    assert winbash.git_bash() is None
    assert "bash" not in asked


@pytest.mark.parametrize("env,rel", [("ProgramFiles", ("Git",)),
                                     ("LOCALAPPDATA", ("Programs", "Git"))])
def test_finds_git_not_on_path(tmp_path, monkeypatch, env, rel):
    base = str(tmp_path)
    bash = _touch(base, *rel, "bin", "bash.exe")
    monkeypatch.setenv(env, base)
    monkeypatch.setattr(winbash.shutil, "which", _which({}, []))
    assert winbash.git_bash() == bash


def test_result_is_cached_including_a_miss(monkeypatch):
    asked = []
    monkeypatch.setattr(winbash.shutil, "which", _which({}, asked))
    assert winbash.git_bash() is None
    assert winbash.git_bash() is None
    assert asked == ["git"]


@pytest.mark.parametrize("given,want", [
    ("/c/Users/x/bin/App.prg", "C:\\Users\\x\\bin\\App.prg"),
    ("/d", "D:\\"),
    ("/d/", "D:\\"),
    (r"C:\already\windows", r"C:\already\windows"),
    ("relative/path", "relative/path"),
])
def test_to_windows_path_drive_forms(given, want):
    assert winbash.to_windows_path(given) == want


def test_msys_root_path_without_bash_is_left_alone(monkeypatch):
    monkeypatch.setattr(winbash, "git_bash", lambda: None)
    assert winbash.to_windows_path("/tmp/out.prg") == "/tmp/out.prg"


def test_msys_root_path_asks_cygpath(tmp_path, monkeypatch):
    root = str(tmp_path)
    bash = _touch(root, "bin", "bash.exe")
    cygpath = _touch(root, "usr", "bin", "cygpath.exe")
    monkeypatch.setattr(winbash, "git_bash", lambda: bash)
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "C:\\Temp\\out.prg\n", "")

    monkeypatch.setattr(winbash.subprocess, "run", run)
    assert winbash.to_windows_path("/tmp/out.prg") == "C:\\Temp\\out.prg"
    assert calls == [[cygpath, "-w", "/tmp/out.prg"]]


def test_cygpath_failure_returns_the_path_unchanged(tmp_path, monkeypatch):
    root = str(tmp_path)
    bash = _touch(root, "bin", "bash.exe")
    _touch(root, "bin", "cygpath.exe")
    monkeypatch.setattr(winbash, "git_bash", lambda: bash)

    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 10)

    monkeypatch.setattr(winbash.subprocess, "run", run)
    assert winbash.to_windows_path("/tmp/out.prg") == "/tmp/out.prg"
