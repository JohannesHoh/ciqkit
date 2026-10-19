"""The runtime directory is a security boundary, so test it like one.

The broker's control channel lives in this directory and a job submitted over
it can run a build script. If the directory is writable by another local
account, that is local code execution. These tests pin the guarantees
ciqkit.paths is supposed to provide -- on POSIX as a 0700 mode, on Windows as a
protected DACL that names nobody but this user and SYSTEM.
"""
import os
import re
import stat
import subprocess
import sys

import pytest

from ciqkit import paths

def _own_trustees() -> set:
    """This user's SID and SYSTEM, as an SDDL string names them. SDDL writes
    well-known accounts by alias: the built-in Administrator (RID 500, which
    GitHub's Windows runners run as) appears as "LA", not as its SID."""
    from ciqkit import _winapi
    sid = _winapi.user_sid()
    return {sid, "SY"} | ({"LA"} if sid.endswith("-500") else set())


IS_WIN = sys.platform == "win32"


@pytest.fixture(autouse=True)
def _private_runtime(tmp_path, monkeypatch):
    """Point XDG_RUNTIME_DIR at a throwaway directory for every test."""
    base = tmp_path / "xdg"
    base.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(base))
    return base


def _mode(p) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def _assert_private(p):
    if not IS_WIN:
        assert _mode(p) == 0o700, f"{p} must be 0700, got {oct(_mode(p))}"
        return
    from ciqkit import _winapi
    sddl = _winapi.dacl_sddl(p)
    # Protected (nothing inherited from a permissive parent) ...
    assert sddl.startswith("D:P"), sddl
    # ... and every ACE grants to this user or SYSTEM, nobody else.
    trustees = set(re.findall(r"\([^;]*;[^;]*;[^;]*;[^;]*;[^;]*;([^)]*)\)", sddl))
    assert trustees <= _own_trustees(), sddl
    assert trustees & (_own_trustees() - {"SY"}), sddl


def _loosen(p):
    """What a permissive umask -- or a helpful admin -- does to a directory."""
    if IS_WIN:
        from ciqkit import _winapi
        # Everyone: full control, inherited from the parent as well.
        _winapi.set_dacl(p, "D:(A;OICI;FA;;;WD)")
    else:
        os.chmod(p, 0o755)


def test_runtime_dir_is_created_private():
    d = paths.runtime_dir()
    assert os.path.isdir(d)
    _assert_private(d)


def test_runtime_dir_is_idempotent():
    assert paths.runtime_dir() == paths.runtime_dir()


def test_loose_permissions_are_tightened():
    d = paths.runtime_dir()
    _loosen(d)
    _assert_private(paths.runtime_dir())


def _link_dir(link, target):
    """A symlink, or on Windows without the symlink privilege a junction --
    the reparse point an unprivileged attacker can actually create there."""
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if not IS_WIN:
            raise
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)


def test_symlink_in_place_of_the_directory_is_rejected(tmp_path, monkeypatch):
    base = tmp_path / "xdg2"
    base.mkdir(mode=0o700)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _link_dir(base / "ciqkit", elsewhere)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(base))

    with pytest.raises(paths.InsecureRuntimeDir):
        paths.runtime_dir()


def test_state_dir_is_private_and_nested():
    d = paths.state_dir("leases")
    assert d.startswith(paths.runtime_dir())
    _assert_private(d)


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "../escape", "a\\b"])
def test_state_dir_rejects_path_traversal(name):
    with pytest.raises(ValueError):
        paths.state_dir(name)


def test_default_out_dir_is_not_the_working_tree():
    """Screenshots must not land in the user's repo unless asked for."""
    out = paths.default_out_dir()
    assert out.startswith(paths.runtime_dir())
    assert os.path.isdir(out)


@pytest.mark.skipif(not IS_WIN, reason="Windows default location")
def test_windows_default_is_in_the_users_own_profile(monkeypatch, tmp_path):
    """Not %TEMP%: a TEMP override can point anywhere, LOCALAPPDATA cannot be
    listed by other non-admin accounts."""
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    (tmp_path / "local").mkdir()
    d = paths.runtime_dir()
    assert d == str(tmp_path / "local" / "ciqkit" / "runtime")
    _assert_private(d)


@pytest.mark.skipif(not IS_WIN, reason="Windows ownership check")
def test_a_directory_owned_by_someone_else_is_refused(monkeypatch):
    from ciqkit import _winapi
    d = paths.runtime_dir()
    monkeypatch.setattr(_winapi, "owned_by_me", lambda p: (False, "S-1-5-21-0-0-0-1002"))
    with pytest.raises(paths.InsecureRuntimeDir, match="owned by"):
        paths._ensure_private(d)
