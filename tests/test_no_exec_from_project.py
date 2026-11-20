"""Nothing from the project or the current directory runs (SECURITY.md §2).

With a pre-built ``--prg`` ciqkit promises to execute nothing from the project.
Four ways that promise used to leak, one section each:

* the broker interpreter was looked for as a ``.venv`` above the cwd, probed,
  run as the daemon and cached;
* the probe and the daemon ran ``python -c`` / ``-m`` in the caller's cwd, so a
  ``json.py`` there was imported into the daemon;
* git, run in the ``.prg``'s checkout, honours that repo's ``core.fsmonitor``
  and filter drivers;
* on Windows a ``git.exe`` / ``java.exe`` in the cwd wins over PATH.
"""
import os
import shutil
import subprocess
import sys

import pytest

from ciqkit import _compat, safegit
from ciqkit.sim import client

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership/exec bits")


def _make_venv(root):
    d = os.path.join(str(root), ".venv", *client._VENV_PYTHON[:-1])
    os.makedirs(d)
    exe = os.path.join(d, client._VENV_PYTHON[-1])
    with open(exe, "wb") as f:
        f.write(b"#!/bin/sh\nexit 0\n")
    os.chmod(exe, 0o755)
    return exe


# ── broker interpreter discovery ─────────────────────────────────────────────

def test_a_venv_in_the_cwd_is_not_a_broker_candidate(tmp_path, monkeypatch):
    planted = _make_venv(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CIQKIT_PYTHON", raising=False)
    assert planted not in client.broker_python_candidates()


def test_a_relative_ciqkit_python_is_anchored_at_the_callers_cwd(tmp_path, monkeypatch):
    # The probe runs from the runtime dir; a relative CIQKIT_PYTHON must still
    # mean the file the user pointed at from where they stood.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIQKIT_PYTHON", os.path.join(".venv", "bin", "python"))
    first = client.broker_python_candidates()[0]
    assert first == os.path.join(str(tmp_path), ".venv", "bin", "python")
    monkeypatch.setenv("CIQKIT_PYTHON", "python3")
    assert client.broker_python_candidates()[0] == "python3"


@posix_only
def test_a_venv_writable_by_others_is_not_trusted(tmp_path):
    exe = _make_venv(tmp_path)
    venv = os.path.join(str(tmp_path), ".venv")
    os.chmod(venv, 0o755)
    os.chmod(os.path.dirname(exe), 0o755)
    assert client._trusted_venv_python(venv) == exe
    os.chmod(venv, 0o777)
    assert client._trusted_venv_python(venv) is None
    os.chmod(venv, 0o755)
    os.chmod(os.path.dirname(exe), 0o775)
    assert client._trusted_venv_python(venv) is None


@posix_only
def test_a_venv_owned_by_someone_else_is_not_trusted(tmp_path, monkeypatch):
    if os.getuid() == 0:
        pytest.skip("root owns everything it creates")
    _make_venv(tmp_path)
    monkeypatch.setattr(client.os, "getuid", lambda: os.stat(str(tmp_path)).st_uid + 1)
    assert client._trusted_venv_python(os.path.join(str(tmp_path), ".venv")) is None


def test_a_cached_interpreter_is_used_only_if_still_a_candidate(tmp_path, monkeypatch):
    """The cache saves the probe; it is not a trust anchor."""
    monkeypatch.setattr(client.proto, "runtime_dir", lambda: str(tmp_path))
    planted = _make_venv(tmp_path / "elsewhere")
    (tmp_path / "broker-python").write_text(planted + "\n")
    probed = []
    monkeypatch.setattr(client, "broker_python_candidates", lambda: ["/usr/bin/works"])
    monkeypatch.setattr(client, "_can_run_broker", lambda p: probed.append(p) or True)

    assert client.resolve_broker_python() == "/usr/bin/works"
    assert planted not in probed


# ── probe and daemon run outside the caller's cwd ────────────────────────────

def test_probe_runs_with_safe_path_from_the_runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(client.proto, "runtime_dir", lambda: str(tmp_path))
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, cwd=kw.get("cwd"))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(client.subprocess, "run", fake_run)
    assert client._can_run_broker("/usr/bin/py")
    assert seen["argv"][:3] == ["/usr/bin/py", "-P", "-c"]
    assert seen["cwd"] == str(tmp_path)


def test_daemon_runs_with_safe_path_from_the_runtime_dir(tmp_path, monkeypatch):
    rundir = tmp_path / "rt"
    rundir.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    monkeypatch.setattr(client.proto, "runtime_dir", lambda: str(rundir))
    monkeypatch.setattr(client, "resolve_broker_python", lambda: "/usr/bin/works")
    seen = {}

    def fake_popen(argv, **kw):
        seen.update(argv=argv, cwd=kw.get("cwd"), env=kw["env"])

    monkeypatch.setattr(client.subprocess, "Popen", fake_popen)
    client.spawn_daemon("rel.sock")
    assert seen["argv"] == ["/usr/bin/works", "-P", "-m", "ciqkit.sim.broker"]
    assert seen["cwd"] == str(rundir)
    # A relative socket path would otherwise be resolved against the new cwd.
    assert (os.path.realpath(seen["env"]["CIQSIM_SOCK"])
            == os.path.realpath(os.path.join(str(proj), "rel.sock")))
    if sys.platform == "win32":
        assert seen["env"]["NoDefaultCurrentDirectoryInExePath"] == "1"


def test_safe_path_keeps_a_planted_module_out(tmp_path):
    """The real interpreter, not a mock: -P is what keeps cwd off sys.path."""
    marker = tmp_path / "imported"
    (tmp_path / "json.py").write_text(
        f"open({str(marker)!r}, 'w').close()\n")
    code = "import json"
    subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path), check=False)
    if not marker.exists():
        pytest.skip("this interpreter does not put cwd on sys.path for -c")
    marker.unlink()
    subprocess.run(client._python_argv(sys.executable, "-c", code), cwd=str(tmp_path),
                   check=False)
    assert not marker.exists()


# ── git never runs the repository's own config ───────────────────────────────

def test_git_argv_carries_the_overrides(monkeypatch):
    monkeypatch.setattr(safegit, "which_no_cwd", lambda n: "/usr/bin/git")

    def fake_run(argv, **kw):
        out = b"filter.evil.clean\0filter.lfs.x.process\0filter.evil.required\0"
        return subprocess.CompletedProcess(argv, 0, stdout=out)

    monkeypatch.setattr(safegit.subprocess, "run", fake_run)
    argv = safegit.argv("/repo", "status", "--porcelain")
    assert argv[:2] == ["/usr/bin/git", "--no-pager"]
    pairs = {argv[i + 1] for i, a in enumerate(argv) if a == "-c"}
    assert {"core.fsmonitor=false", f"core.hooksPath={os.devnull}",
            "protocol.ext.allow=never", "filter.evil.clean=", "filter.evil.process=",
            "filter.evil.required=false", "filter.lfs.x.process="} <= pairs
    assert argv[-3:] == ["status", "--ignore-submodules=all", "--porcelain"]

    diff = safegit.argv("/repo", "diff", "--quiet", "--", "f")
    assert diff[-6:] == ["diff", "--no-ext-diff", "--no-textconv", "--quiet", "--", "f"]
    # No content comparison, no filter lookup needed -- but the base overrides stay.
    rp = safegit.argv("/repo", "rev-parse", "HEAD")
    assert "core.fsmonitor=false" in rp and rp[-2:] == ["rev-parse", "HEAD"]


def test_a_filter_name_that_cannot_be_overridden_is_refused(monkeypatch):
    monkeypatch.setattr(safegit, "which_no_cwd", lambda n: "/usr/bin/git")
    monkeypatch.setattr(safegit.subprocess, "run", lambda argv, **kw:
                        subprocess.CompletedProcess(argv, 0, stdout=b"filter.a=b.clean\0"))
    with pytest.raises(safegit.UnsafeRepo):
        safegit.argv("/repo", "status")


@posix_only
@pytest.mark.parametrize("name", [b"a\x0cb", b"a\xe2\x80\xa8b", b"a\x85b", b"x\xffy", b""])
def test_odd_filter_driver_names_are_neutralised(monkeypatch, name):
    # splitlines() cut these apart, a lossy decode renamed them, and the empty
    # name was dropped: each left the repo's own clean filter live.
    monkeypatch.setattr(safegit, "which_no_cwd", lambda n: "/usr/bin/git")
    out = b"filter." + name + b".clean\0"
    monkeypatch.setattr(safegit.subprocess, "run", lambda argv, **kw:
                        subprocess.CompletedProcess(argv, 0, stdout=out))
    argv = safegit.argv("/repo", "status")
    want = "filter." + os.fsdecode(name) + ".clean="
    assert want in argv


@pytest.mark.parametrize("key", [b"extensions.partialclone", b"remote.origin.promisor",
                                 b"remote.a.b.promisor",
                                 b"remote.evil.partialclonefilter"])
@pytest.mark.parametrize("version,refused", [("2.39.5", True), ("2.43.0", True),
                                             ("2.44.0", False), ("2.54.1", False),
                                             ("garbage", True)])
def test_a_partial_clone_is_refused_on_git_without_no_lazy_fetch(monkeypatch, key,
                                                                 version, refused):
    # GIT_NO_LAZY_FETCH is ignored before git 2.44, and a repo that sets
    # protocol.<name>.allow=always plus alias.remote-<name> beats the
    # `protocol.allow=never` fallback.
    monkeypatch.setattr(safegit, "which_no_cwd", lambda n: "/usr/bin/git")

    def fake_run(argv, **kw):
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=f"git version {version}\n")
        return subprocess.CompletedProcess(argv, 0, stdout=key + b"\0")

    monkeypatch.setattr(safegit.subprocess, "run", fake_run)
    if refused:
        with pytest.raises(safegit.UnsafeRepo):
            safegit.argv("/repo", "status")
    else:
        assert "--ignore-submodules=all" in safegit.argv("/repo", "status")
    # Commands that never read blobs are not affected.
    assert safegit.argv("/repo", "rev-parse", "HEAD")[-2:] == ["rev-parse", "HEAD"]


def test_no_git_is_a_plain_os_error(monkeypatch):
    monkeypatch.setattr(safegit, "which_no_cwd", lambda n: None)
    with pytest.raises(FileNotFoundError):
        safegit.run("/repo", "rev-parse", "HEAD")


@pytest.fixture
def hostile_repo(tmp_path):
    """A repo whose .git/config runs a script from core.fsmonitor and a clean filter."""
    if not shutil.which("git"):
        pytest.skip("git not installed")
    repo, marks = tmp_path / "repo", tmp_path / "marks"
    (repo / "bin").mkdir(parents=True)
    marks.mkdir()
    hook = tmp_path / "hook.sh"
    hook.write_bytes(f'#!/bin/sh\ntouch "{marks}/$1"\ncat\n'.encode())
    hook.chmod(0o755)

    def git(*args):
        subprocess.run(["git", *args], cwd=str(repo), capture_output=True, check=True)

    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (repo / ".gitattributes").write_bytes(b"* filter=evil\n")
    (repo / "bin" / "App.prg").write_bytes(b"prg")
    (repo / "f.txt").write_bytes(b"a\n")
    git("add", "-A")
    git("commit", "-qm", "initial")
    git("config", "core.fsmonitor", f"{hook} fsmonitor")
    git("config", "filter.evil.clean", f"{hook} clean")
    return repo, marks


def _restat(repo):
    # A changed mtime makes git re-read the content -- through the clean filter.
    for name in ("f.txt", os.path.join("bin", "App.prg")):
        st = os.stat(repo / name)
        os.utime(repo / name, (st.st_atime, st.st_mtime - 3600))


@posix_only
@pytest.mark.parametrize("vector,control", [
    ("fsmonitor", []),
    # fsmonitor off, so only the filter can leave its mark.
    ("clean", ["-c", "core.fsmonitor=false"]),
])
def test_git_in_a_hostile_checkout_runs_nothing(hostile_repo, vector, control):
    from ciqkit.sim import seed, storage

    repo, marks = hostile_repo
    # Control: plain git does run it, or this test would prove nothing.
    _restat(repo)
    subprocess.run(["git", *control, "status", "--porcelain"], cwd=str(repo),
                   capture_output=True)
    if not (marks / vector).exists():
        pytest.skip(f"this git does not run {vector} even unhardened")
    for m in marks.iterdir():
        m.unlink()

    _restat(repo)
    info = storage._git_info(str(repo / "bin" / "App.prg"))
    assert info.get("sha") and info.get("root")
    _restat(repo)
    assert seed._git_status(str(repo / "f.txt")) in ("clean", "dirty")
    assert sorted(p.name for p in marks.iterdir()) == []


# ── executables are never taken from the cwd ─────────────────────────────────

def _exe(d, name):
    d.mkdir(parents=True, exist_ok=True)
    for n in (name, name + ".exe"):
        p = d / n
        p.write_bytes(b"#!/bin/sh\n")
        p.chmod(0o755)
    return str(d)


def test_which_skips_the_cwd_and_relative_path_entries(tmp_path, monkeypatch):
    cwd = _exe(tmp_path / "checkout", "tool")
    good = _exe(tmp_path / "usr" / "bin", "tool")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATHEXT", ".EXE")
    monkeypatch.setenv("PATH", os.pathsep.join([".", "relative", good]))
    found = _compat.which_no_cwd("tool")
    assert found and os.path.dirname(found) == good
    monkeypatch.setenv("PATH", os.pathsep.join([".", ""]))
    assert _compat.which_no_cwd("tool") is None


@posix_only
@pytest.mark.parametrize("name", [b"a\x0cb", b"x\xffy", b"a\xe2\x80\xa8b", b""])
def test_odd_named_clean_filter_in_a_real_repo_runs_nothing(hostile_repo, name):
    from ciqkit.sim import storage

    repo, marks = hostile_repo
    hook = repo.parent / "hook.sh"
    cfg = repo / ".git" / "config"
    cfg.write_bytes(cfg.read_bytes() + b'[filter "' + name + b'"]\n\tclean = "'
                    + str(hook).encode() + b' odd"\n')
    (repo / ".gitattributes").write_bytes(b"* filter=" + name + b"\n")
    _restat(repo)
    subprocess.run(["git", "-c", "core.fsmonitor=false", "status", "--porcelain"],
                   cwd=str(repo), capture_output=True)
    if not (marks / "odd").exists():
        pytest.skip("this git does not run this filter name even unhardened")
    (marks / "odd").unlink()
    _restat(repo)
    assert storage._git_info(str(repo / "bin" / "App.prg")).get("sha")
    assert not (marks / "odd").exists()


def test_relative_path_settings_reach_the_daemon_absolute(tmp_path, monkeypatch):
    # The daemon runs from the runtime dir; CIQKIT_DEV_KEY=developer_key.der set
    # in a project must still mean that project's key.
    rundir = tmp_path / "rt"
    rundir.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    monkeypatch.setattr(client.proto, "runtime_dir", lambda: str(rundir))
    monkeypatch.setattr(client, "resolve_broker_python", lambda: "/usr/bin/works")
    monkeypatch.setenv("CIQKIT_DEV_KEY", "developer_key.der")
    absolute_home = os.path.join(str(tmp_path), "sdk")
    monkeypatch.setenv("CIQ_HOME", absolute_home)
    seen = {}
    monkeypatch.setattr(client.subprocess, "Popen",
                        lambda argv, **kw: seen.update(env=kw["env"]))
    client.spawn_daemon(os.path.join(str(rundir), "s.sock"))
    assert (os.path.realpath(seen["env"]["CIQKIT_DEV_KEY"])
            == os.path.realpath(os.path.join(str(proj), "developer_key.der")))
    assert seen["env"]["CIQ_HOME"] == absolute_home


def test_a_bare_ciqkit_python_on_windows_is_resolved_on_path_only(monkeypatch):
    monkeypatch.setattr(client, "IS_WIN", True)
    # The venv-ownership check would reach real Win32 APIs; not what this tests.
    monkeypatch.setattr(client, "_trusted_venv_python", lambda venv: None)
    monkeypatch.setenv("CIQKIT_PYTHON", "py")
    monkeypatch.setattr(client, "which_no_cwd",
                        lambda name: r"C:\Windows\py.exe" if name == "py" else None)
    assert client.broker_python_candidates()[0] == r"C:\Windows\py.exe"
    # Not on PATH: dropped, never handed to CreateProcess as a bare name.
    monkeypatch.setenv("CIQKIT_PYTHON", "nosuch")
    assert "nosuch" not in client.broker_python_candidates()
