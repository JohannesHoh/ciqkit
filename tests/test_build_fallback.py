"""Built-in monkeyc build, developer key, keygen and default device. No SDK,
simulator or display: everything runs against tmp dirs."""
import json
import os
import shutil
import stat
import sys

import pytest

from ciqkit import build
from ciqkit.sim import session as sess

MONKEYC = "monkeyc.bat" if sys.platform == "win32" else "monkeyc"


@pytest.fixture
def sdk(tmp_path, monkeypatch):
    d = tmp_path / "sdk"
    (d / "bin").mkdir(parents=True)
    # Both launchers: the SDK ships bin/monkeyc.bat on Windows.
    for name in ("monkeyc", "monkeyc.bat"):
        (d / "bin" / name).write_text("#!/bin/sh\n")
    monkeypatch.setenv("CIQ_HOME", str(d))
    return d


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "MyApp"
    p.mkdir()
    (p / "monkey.jungle").write_text("")
    return p


@pytest.fixture
def key(tmp_path, monkeypatch):
    k = tmp_path / "k.der"
    k.write_bytes(b"x")
    monkeypatch.setenv("CIQKIT_DEV_KEY", str(k))
    return k


def test_fallback_command_line(sdk, proj, key):
    argv = build.monkeyc_argv(str(proj), "fenix7")
    assert argv == [str(sdk / "bin" / MONKEYC), "-f", "monkey.jungle", "-d", "fenix7",
                    "-y", str(key), "-o", os.path.join("bin", "MyApp.prg")]


def test_test_variant(sdk, proj, key):
    argv = build.monkeyc_argv(str(proj), "fenix7", tests=True)
    assert argv[-1] == "-t" and os.path.join("bin", "MyApp-test.prg") in argv


def test_no_jungle_or_sdk_raises(sdk, tmp_path, key, monkeypatch):
    empty = tmp_path / "e"
    empty.mkdir()
    with pytest.raises(build.BuildError, match="monkey.jungle"):
        build.monkeyc_argv(str(empty), "venu3")
    monkeypatch.setenv("CIQ_HOME", str(tmp_path / "nowhere"))
    with pytest.raises(build.BuildError, match="SDK"):
        build.monkeyc_argv(str(empty), "venu3")


def test_precedence(sdk, proj, key, monkeypatch):
    monkeypatch.delenv("CIQKIT_BUILD_CMD", raising=False)
    argv, tmpl = build.resolve_build(str(proj), "venu3")
    assert argv and not tmpl
    (proj / "scripts").mkdir()
    (proj / "scripts" / "build.sh").write_text("")
    assert build.resolve_build(str(proj), "venu3") == (None, build.PROJECT_BUILD_CMD)
    monkeypatch.setenv("CIQKIT_BUILD_CMD", "make {device}")
    assert build.resolve_build(str(proj), "venu3") == (None, "make {device}")
    assert build.resolve_build(str(proj), "venu3", build_cmd="x") == (None, "x")


def test_session_build_runs_monkeyc_fallback(sdk, proj, key, monkeypatch):
    monkeypatch.delenv("CIQKIT_BUILD_CMD", raising=False)
    seen = {}

    class R:
        returncode = 0
        stdout = "wrote bin/MyApp.prg"
        stderr = ""

    def fake_run(argv, **kw):
        seen["argv"], seen["cwd"] = argv, kw["cwd"]
        os.makedirs(proj / "bin", exist_ok=True)
        (proj / "bin" / "MyApp.prg").write_text("p")
        return R()

    monkeypatch.setattr(sess.subprocess, "run", fake_run)
    out = sess.SimSession.__new__(sess.SimSession).build(str(proj), "venu3")
    assert out.endswith("bin/MyApp.prg")
    assert seen["argv"][0] == str(sdk / "bin" / MONKEYC) and seen["cwd"] == str(proj)


def test_missing_key_says_keygen(tmp_path, monkeypatch):
    monkeypatch.delenv("CIQKIT_DEV_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    with pytest.raises(build.DevKeyMissing, match="ciqkit keygen"):
        build.resolve_dev_key()
    assert build.dev_key_path() == str(tmp_path / "cfg/ciqkit/developer_key.der")


def test_env_key_overrides(key):
    assert build.resolve_dev_key() == str(key)


needs_openssl = pytest.mark.skipif(not shutil.which("openssl"), reason="no openssl")


@needs_openssl
def test_keygen_perms_and_refuse_overwrite(tmp_path, monkeypatch, capsys):
    from ciqkit import keygen_cli
    target = tmp_path / "cfg" / "ciqkit" / "developer_key.der"
    monkeypatch.setenv("CIQKIT_DEV_KEY", str(target))
    real = build.generate_dev_key
    monkeypatch.setattr(build, "generate_dev_key",
                        lambda path=None, bits=4096: real(path, bits=1024))
    assert keygen_cli.main([]) == 0
    assert json.loads(capsys.readouterr().out)["key"] == str(target)
    if sys.platform != "win32":     # Windows has no mode bits to check
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert target.read_bytes()[:1] == b"\x30"          # DER SEQUENCE
    assert os.listdir(target.parent) == [target.name]   # no PEM / temp left
    before = target.read_bytes()
    assert keygen_cli.main([]) == 1
    assert "already exists" in capsys.readouterr().err
    assert target.read_bytes() == before


def test_keygen_dispatch_is_registered():
    from ciqkit.cli import _COMMANDS
    assert _COMMANDS["keygen"][0] == "ciqkit.keygen_cli"


# --- default device ---------------------------------------------------------

MANIFEST = """<?xml version="1.0"?>
<iq:manifest xmlns:iq="http://www.garmin.com/xml/connectiq" version="3">
 <iq:application id="x" type="watchface"><iq:products>
  <iq:product id="fenix7"/><iq:product id="venu2"/></iq:products></iq:application>
</iq:manifest>"""


def _devs(tmp_path, *ids):
    d = tmp_path / "devs"
    d.mkdir(exist_ok=True)
    for i in ids:
        (d / i).mkdir()
    return str(d)


def test_device_first_installed(proj, tmp_path):
    (proj / "manifest.xml").write_text(MANIFEST)
    assert build.default_device(str(proj), _devs(tmp_path, "venu2")) == "venu2"
    assert build.default_device(str(proj), _devs(tmp_path, "fenix7")) == "fenix7"


def test_device_none_installed_falls_back(proj, tmp_path):
    (proj / "manifest.xml").write_text(MANIFEST)
    assert build.default_device(str(proj), _devs(tmp_path, "other")) == "venu3"


def test_device_no_or_malformed_manifest(proj, tmp_path):
    d = _devs(tmp_path, "venu3")
    assert build.default_device(str(proj), d) == "venu3"
    (proj / "manifest.xml").write_text("<iq:manifest")
    assert build.default_device(str(proj), d) == "venu3"


def test_device_unreadable_sdk_dir_uses_manifest_first(proj, tmp_path):
    (proj / "manifest.xml").write_text(MANIFEST)
    assert build.default_device(str(proj), str(tmp_path / "missing")) == "fenix7"
