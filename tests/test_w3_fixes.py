"""Interpreter cache keyed on install, lease holder encoding, smoke sha fallback,
broker install reporting."""
import importlib.util
import json
import os
import sys

from ciqkit import doctor, leases
from ciqkit.sim import client


def _probe(monkeypatch, ok):
    monkeypatch.setattr(client, "_can_run_broker", lambda p: p in ok)


def test_explicit_python_beats_cache_and_sys_executable(monkeypatch):
    monkeypatch.setenv("CIQKIT_PYTHON", "explicit")
    with open(client._cached_broker_python_path(), "w", encoding="utf-8") as f:
        json.dump({"python": "cached", "ciqkit_path": client._install_dir()}, f)
    _probe(monkeypatch, {"explicit", "cached", sys.executable})
    assert client.resolve_broker_python() == "explicit"


def test_sys_executable_beats_cache(monkeypatch):
    monkeypatch.delenv("CIQKIT_PYTHON", raising=False)
    with open(client._cached_broker_python_path(), "w", encoding="utf-8") as f:
        json.dump({"python": "cached", "ciqkit_path": client._install_dir()}, f)
    _probe(monkeypatch, {"cached", sys.executable})
    assert client.resolve_broker_python() == sys.executable


def test_cache_for_other_install_or_old_format_is_a_miss(monkeypatch, tmp_path):
    monkeypatch.delenv("CIQKIT_PYTHON", raising=False)
    other = tmp_path / "other-python"
    other.write_bytes(b"")
    cache = client._cached_broker_python_path()
    with open(cache, "w", encoding="utf-8") as f:
        json.dump({"python": str(other), "ciqkit_path": os.path.join("elsewhere", "ciqkit")}, f)
    assert client._read_cached_python() is None
    with open(cache, "w", encoding="utf-8") as f:
        f.write(str(other))                      # old plain-text format
    assert client._read_cached_python() is None
    # sys.executable can't run it, the cache is a miss, so a candidate wins and
    # is written in the new format for this install.
    monkeypatch.setattr(client, "broker_python_candidates", lambda: ["cand"])
    _probe(monkeypatch, {"cand"})
    assert client.resolve_broker_python() == "cand"
    assert client._read_cached_python() == "cand"


def test_ping_reports_install_and_doctor_warns_on_mismatch():
    import ciqkit
    from ciqkit.sim import broker
    own = os.path.dirname(os.path.abspath(ciqkit.__file__))
    assert doctor.broker_install_check({"install": own, "pid": 1}, own) is None
    assert doctor.broker_install_check({"pid": 1}, own) is None   # old broker
    w = doctor.broker_install_check(
        {"install": os.path.join("x", "ciqkit"), "pid": 7, "version": "0.1"}, own)
    assert w["status"] == "warn" and "pid 7" in w["detail"]
    assert broker.Broker.__name__   # module imports


def test_lease_holder_utf8_roundtrip_and_clean_unknown(tmp_path):
    with leases.lease("utf", timeout=5, meta={"who": "Jürgen ✓"}):
        _, hp = leases._paths("utf")
        assert leases._read_holder(hp)["who"] == "Jürgen ✓"
    p = tmp_path / "bad.holder"
    p.write_bytes(b'{"who": "caf\xe9"}')            # not valid UTF-8
    assert "�" in leases._read_holder(str(p))["who"]
    p.write_bytes(b'{"who": null}')
    assert (leases._read_holder(str(p)).get("who") or "unknown") == "unknown"
    p.write_bytes(b'[1]')
    assert leases._read_holder(str(p)) is None


def test_lease_survives_a_surrogate_who(tmp_path):
    who = "caf\udce9"       # lone surrogate, as a non-UTF-8 argv byte decodes on POSIX
    with leases.lease("sur", timeout=5, meta={"who": who}):
        _, hp = leases._paths("sur")
        assert leases._read_holder(hp) is not None


def test_smoke_sha_falls_back_when_git_unreadable(tmp_path, monkeypatch):
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts", "sim_smoke.py")
    spec = importlib.util.spec_from_file_location("sim_smoke_w3", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    (git / "refs" / "heads" / "main").write_bytes(b"abcdef1234567890\n")
    monkeypatch.setattr(mod, "ROOT", str(tmp_path))
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert mod._git_sha() == "abcdef1"
    (git / "HEAD").unlink()
    assert mod._git_sha().startswith("v")


def test_only_stock_scales_are_candidates():
    """A free continuous (width-ratio) scale let another device's art pass the
    agreement floor on real device PNGs, so only the hint, 1 and 2 are tried."""
    from ciqkit.capture import framing

    assert framing._scale_candidates(1.0) == [1.0, 2.0]
    assert framing._scale_candidates(0.5) == [0.5, 1.0, 2.0]
    assert framing._scale_candidates(None) == [1.0, 2.0]
    assert framing._scale_candidates("bad") == [1.0, 2.0]
