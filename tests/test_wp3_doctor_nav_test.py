"""WP3: ciqsim_nav / ciqsim_doctor, `doctor --project`, device defaults at the
edge, and `sim test` building its own -t variant. Fakes only."""
import json
import os
import types

import pytest

from ciqkit import build, doctor, cli
from ciqkit.sim import client, mcp_server
from ciqkit.sim import session as sess
from ciqkit.sim.protocol import ChunkKind, Priority

NS = "http://www.garmin.com/xml/connectiq"


def _manifest(app_id="abc-123", products=("venu3", "fenix7")):
    prods = "".join(f'<iq:product id="{p}"/>' for p in products)
    app = f' id="{app_id}"' if app_id else ""
    return (f'<iq:manifest xmlns:iq="{NS}"><iq:application{app} type="watchface">'
            f"<iq:products>{prods}</iq:products></iq:application></iq:manifest>")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """SDK with monkeyc, device dir, key; no build env override."""
    sdk = tmp_path / "sdk"
    (sdk / "bin").mkdir(parents=True)
    for name in ("monkeyc", "monkeyc.bat"):     # .bat: the Windows SDK's launcher
        (sdk / "bin" / name).write_text("")
    monkeypatch.setenv("CIQ_HOME", str(sdk))
    devs = tmp_path / "devices"
    (devs / "venu3").mkdir(parents=True)
    monkeypatch.setattr(build, "sdk_devices_dir", lambda: str(devs))
    key = tmp_path / "k.der"
    key.write_bytes(b"x")
    monkeypatch.setenv("CIQKIT_DEV_KEY", str(key))
    monkeypatch.delenv("CIQKIT_BUILD_CMD", raising=False)
    return types.SimpleNamespace(sdk=sdk, devs=devs, key=key)


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "MyApp"
    (p / "source").mkdir(parents=True)
    (p / "manifest.xml").write_text(_manifest())
    (p / "monkey.jungle").write_text("project.manifest = manifest.xml\n")
    (p / "source" / "T.mc").write_text("(:test)\nfunction t(l) { return true; }\n")
    return p


def _by_name(checks):
    return {c["name"]: c for c in checks}


# --- doctor --project --------------------------------------------------------

def test_project_checks_all_green(env, proj):
    c = _by_name(doctor.project_checks(str(proj)))
    assert c["manifest"]["status"] == "ok" and "abc-123" in c["manifest"]["detail"]
    assert c["devices"]["status"] == "warn"     # fenix7 not installed
    assert "fenix7" in c["devices"]["notes"][0]
    assert c["dev key"]["status"] == "ok"
    assert c["build"]["status"] == "ok" and "built-in monkeyc" in c["build"]["detail"]
    assert c["monkey.jungle"]["status"] == "ok"
    assert c["tests"]["status"] == "ok"


def test_no_installed_product_is_a_failure(env, proj):
    (env.devs / "venu3").rmdir()
    c = _by_name(doctor.project_checks(str(proj)))
    assert c["devices"]["status"] == "fail"


def test_bad_manifests(env, tmp_path):
    p = tmp_path / "Bad"
    p.mkdir()
    assert _by_name(doctor.project_checks(str(p)))["manifest"]["status"] == "fail"
    (p / "manifest.xml").write_text("<not xml")
    assert "does not parse" in _by_name(doctor.project_checks(str(p)))["manifest"]["detail"]
    (p / "manifest.xml").write_text(_manifest(app_id=None, products=()))
    d = _by_name(doctor.project_checks(str(p)))["manifest"]["detail"]
    assert "app UUID" in d and "<iq:product>" in d


def test_missing_key_and_sdk_fail_the_build_route(env, proj, monkeypatch):
    monkeypatch.setenv("CIQKIT_DEV_KEY", str(env.key) + ".gone")
    c = _by_name(doctor.project_checks(str(proj)))
    assert c["dev key"]["status"] == "fail" and "keygen" in c["dev key"]["detail"]
    assert c["build"]["status"] == "fail"


def test_build_route_labels(env, proj, monkeypatch):
    assert build.build_route(str(proj)) == "built-in monkeyc"
    (proj / "scripts").mkdir()
    (proj / "scripts" / "build.sh").write_text("")
    assert build.build_route(str(proj)).startswith("build.sh")
    monkeypatch.setenv("CIQKIT_BUILD_CMD", "make {device}")
    c = _by_name(doctor.project_checks(str(proj)))
    assert c["build"]["detail"].startswith("env")


def test_tests_warn_when_missing_or_unreachable(env, proj):
    (proj / "source" / "T.mc").write_text("function f() {}\n")
    assert _by_name(doctor.project_checks(str(proj)))["tests"]["status"] == "warn"
    (proj / "source" / "T.mc").unlink()
    (proj / "tests").mkdir()
    (proj / "tests" / "T.mc").write_text("(:test)\nfunction t(l) {}\n")
    assert _by_name(doctor.project_checks(str(proj)))["tests"]["status"] == "warn"
    (proj / "monkey.jungle").write_text("base.sourcePath = source;tests\n")
    assert _by_name(doctor.project_checks(str(proj)))["tests"]["status"] == "ok"


def test_missing_jungle_fails_only_for_builtin(env, proj):
    (proj / "monkey.jungle").unlink()
    assert _by_name(doctor.project_checks(str(proj)))["monkey.jungle"]["status"] == "fail"


@pytest.fixture
def fast_env(monkeypatch):
    """Skip the real preflight / console-script probes."""
    from ciqkit.sim import preflight
    monkeypatch.setattr(preflight, "check_once", lambda: {"warnings": []})
    monkeypatch.setattr(doctor, "check_console_scripts", lambda: (True, []))


def test_run_without_project_has_no_project_checks(fast_env):
    r = doctor.run()
    assert "manifest" not in _by_name(r["checks"])


def test_run_with_project_and_exit_code(fast_env, env, proj, capsys):
    r = doctor.run(str(proj))
    assert r["ok"] is True and "manifest" in _by_name(r["checks"])
    assert cli.main(["doctor", "--project", str(proj)]) == 0
    out = capsys.readouterr().out
    assert "manifest" in out and "built-in monkeyc" in out
    (env.devs / "venu3").rmdir()
    assert cli.main(["doctor", "--project", str(proj)]) == 1


# --- ciqsim_doctor -----------------------------------------------------------

def test_mcp_doctor_returns_structured_data_without_a_broker(
        fast_env, env, proj, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("doctor must not start a broker")
    monkeypatch.setattr("ciqkit.sim.client.spawn_daemon", boom)
    out = json.loads(mcp_server._call_tool("ciqsim_doctor", {"project": str(proj)}))
    assert out["ok"] is True
    assert _by_name(out["checks"])["manifest"]["status"] == "ok"
    env_only = json.loads(mcp_server._call_tool("ciqsim_doctor", {}))
    assert "manifest" not in _by_name(env_only["checks"])


# --- ciqsim_nav / device default --------------------------------------------

class FakeClient:
    jobs = []
    result = {"ok": True, "job": {"id": "j", "results": []}}

    def __init__(self, *a, **k):
        pass

    def submit_wait(self, job, timeout_s=0):
        FakeClient.jobs.append(job)
        return FakeClient.result


@pytest.fixture
def fake_client(monkeypatch):
    FakeClient.jobs = []
    FakeClient.result = {"ok": True, "job": {"id": "j", "results": []}}
    monkeypatch.setattr(mcp_server, "Client", FakeClient)
    return FakeClient


def test_nav_builds_the_same_job_as_the_cli(env, proj, fake_client):
    steps = [{"kind": "tap", "x": 0.5, "y": 0.8}]
    mcp_server._call_tool("ciqsim_nav", {"project": str(proj), "to": "menu",
                                         "steps": steps, "anchor": ["Menu"],
                                         "force": True})
    job = fake_client.jobs[0]
    load, reach = job.chunks
    assert load.kind == ChunkKind.LOAD and reach.kind == ChunkKind.REACH
    assert load.args["app_dir"] == str(proj) and load.args["force"] is True
    assert reach.args == {"screen": "menu", "steps": steps, "anchor": ["Menu"]}
    # budget matches the CLI formula
    assert reach.max_s == max(60.0, 20.0 + 8.0 * len(steps) * 3)
    assert job.label == "nav menu"


def test_nav_ok_is_the_reach_verdict(env, proj, fake_client):
    fake_client.result = {"ok": True, "job": {"id": "j", "results": [
        {"kind": "reach", "result": {"ok": False, "error": "tap missed"}}]}}
    out = json.loads(mcp_server._call_tool(
        "ciqsim_nav", {"project": str(proj), "to": "x"}))
    assert out["ok"] is False and out["error"] == "tap missed"
    fake_client.result = {"ok": True, "job": {"id": "j", "results": [
        {"kind": "reach", "result": {"ok": True, "anchor": False, "screen": "x"}}]}}
    out = json.loads(mcp_server._call_tool(
        "ciqsim_nav", {"project": str(proj), "to": "x"}))
    assert out["ok"] is False and "expected text" in out["error"]


def test_nav_requires_to_and_list_steps(env, proj, fake_client):
    with pytest.raises(ValueError, match="`to`"):
        mcp_server._call_tool("ciqsim_nav", {"project": str(proj)})
    with pytest.raises(ValueError, match="list"):
        mcp_server._call_tool("ciqsim_nav", {"project": str(proj), "to": "x",
                                             "steps": "nope"})


def test_mcp_device_defaults_to_the_manifest_not_venu3(env, tmp_path, fake_client):
    p = tmp_path / "W"
    p.mkdir()
    (env.devs / "fenix7").mkdir()
    (p / "manifest.xml").write_text(_manifest(products=("fenix7",)))
    mcp_server._call_tool("ciqsim_nav", {"project": str(p), "to": "x"})
    assert fake_client.jobs[0].device == "fenix7"
    assert fake_client.jobs[0].chunks[0].args["device"] == "fenix7"
    mcp_server._call_tool("ciqsim_nav", {"project": str(p), "to": "x",
                                         "device": "venu3"})
    assert fake_client.jobs[1].device == "venu3"


# --- sim test builds the -t variant -----------------------------------------

def test_mcp_test_without_prg_asks_the_broker_to_build(env, proj, fake_client):
    mcp_server._call_tool("ciqsim_test", {"project": str(proj)})
    job = fake_client.jobs[0]
    (chunk,) = job.chunks
    assert chunk.kind == ChunkKind.TEST_BATCH and job.priority == Priority.BATCH
    assert chunk.args["app_dir"] == str(proj) and "prg" not in chunk.args
    mcp_server._call_tool("ciqsim_test", {"project": str(proj), "prg": "bin/a.prg"})
    assert fake_client.jobs[1].chunks[0].args["prg"] == str(proj / "bin" / "a.prg")


def test_cli_test_without_prg_submits_app_dir(env, proj, monkeypatch, capsys):
    jobs = []

    class C:
        def __init__(self, *a, **k):
            pass

        def submit_wait(self, job, timeout_s=0):
            jobs.append(job)
            return {"ok": True, "job": {"id": "j"}}

    monkeypatch.setattr(client, "Client", C)
    assert client.main(["test", "--project", str(proj)]) == 0
    assert jobs[0].chunks[0].args["app_dir"] == str(proj)
    assert jobs[0].chunks[0].args["device"] == "venu3"
    client.main(["test", "--project", str(proj), "--prg", "bin/t.prg"])
    assert jobs[1].chunks[0].args["prg"] == str(proj / "bin" / "t.prg")


def test_session_build_tests_uses_the_t_variant(env, proj, monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        os.makedirs(proj / "bin", exist_ok=True)
        (proj / "bin" / "MyApp-test.prg").write_text("p")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sess.subprocess, "run", fake_run)
    out = sess.SimSession.__new__(sess.SimSession).build(str(proj), "venu3", tests=True)
    assert out.endswith(os.path.join("bin", "MyApp-test.prg")) and seen["argv"][-1] == "-t"


def test_session_build_tests_refuses_a_custom_build_command(env, proj, monkeypatch):
    monkeypatch.setenv("CIQKIT_BUILD_CMD", "make {device}")
    monkeypatch.setattr(sess.subprocess, "run",
                        lambda *a, **k: pytest.fail("must not run a normal build"))
    with pytest.raises(sess.SimError, match="--prg"):
        sess.SimSession.__new__(sess.SimSession).build(str(proj), "venu3", tests=True)


def test_broker_builds_before_running_tests(env, proj):
    from ciqkit.sim.broker import Broker
    from ciqkit.sim.protocol import Chunk, Job

    calls = []

    class S:
        def build(self, app_dir, device, timeout_s=0, tests=False):
            calls.append(("build", app_dir, device, tests))
            return "/x/bin/MyApp-test.prg"

        def run_tests(self, prg, device, timeout_s=0):
            calls.append(("run", prg, device))
            return {"ok": True}

    b = Broker()
    b.session = S()
    chunk = Chunk(ChunkKind.TEST_BATCH, {"app_dir": str(proj), "device": "venu3"},
                  expected_s=1, max_s=5)
    b._dispatch(Job(app="a", chunks=[chunk]), chunk)
    assert calls == [("build", str(proj), "venu3", True),
                     ("run", "/x/bin/MyApp-test.prg", "venu3")]


def test_tests_warn_when_a_custom_build_has_no_test_variant(env, proj):
    """`sim test` refuses a custom build without [build] test_cmd at run time;
    doctor should say so before that."""
    (proj / "scripts").mkdir()
    (proj / "scripts" / "build.sh").write_text("#!/bin/sh\n")
    c = _by_name(doctor.project_checks(str(proj)))
    assert c["tests"]["status"] == "warn" and "test_cmd" in c["tests"]["detail"]
    (proj / "ciqkit.toml").write_text('[build]\ntest_cmd = "./scripts/build.sh -t"\n')
    assert _by_name(doctor.project_checks(str(proj)))["tests"]["status"] == "ok"
