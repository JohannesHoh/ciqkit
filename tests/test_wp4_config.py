"""WP4: ciqkit.toml, precedence, `sim shots`. Fakes only (no sim, SDK, display)."""
import json
import logging
import sys

import pytest

from ciqkit import build, config
from ciqkit.sim import client, mcp_server
from ciqkit.sim.protocol import ChunkKind

NS = "http://www.garmin.com/xml/connectiq"

FULL = '''
# project config
[build]
cmd = "make dev DEVICE={device}"
test_cmd = 'make test DEVICE={device}'

[sim]
device = "fenix7"

[[screens]]
name = "home"
anchor = "12:00"

[[screens]]
name = "settings"
steps = [
  {kind = "tap", x = 0.5, y = 0.8},
  {kind = "swipe", x0 = 0.5, y0 = 0.8, x1 = 0.5, y1 = 0.2},   # trailing comment
]
anchor = ["Settings", "Units"]
seed = ["SEED_DEMO=true", "SEED_PAGE=2"]
storage = "fixtures/full"
'''


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "App"
    p.mkdir()
    (p / "manifest.xml").write_text(
        f'<iq:manifest xmlns:iq="{NS}"><iq:application id="a" type="watchface">'
        f'<iq:products><iq:product id="venu3"/></iq:products></iq:application>'
        f"</iq:manifest>")
    return p


def _write(proj, text):
    (proj / "ciqkit.toml").write_text(text)


def _check_full(cfg):
    assert cfg.build_cmd == "make dev DEVICE={device}"
    assert cfg.build_test_cmd == "make test DEVICE={device}"
    assert cfg.device == "fenix7"
    assert [s.name for s in cfg.screens] == ["home", "settings"]
    home, st = cfg.screens
    assert home.anchor == ["12:00"] and home.steps == [] and home.seed == {}
    assert st.anchor == ["Settings", "Units"]
    assert st.seed == {"SEED_DEMO": "true", "SEED_PAGE": "2"}
    assert st.storage == "fixtures/full"
    assert st.steps[0] == {"kind": "tap", "x": 0.5, "y": 0.8}
    assert st.steps[1]["y1"] == 0.2


@pytest.mark.parametrize("fallback", [False, True])
def test_full_schema(proj, fallback):
    _write(proj, FULL)
    _check_full(config.load_config(str(proj), force_fallback=fallback))


def test_missing_file_is_empty_config(proj):
    cfg = config.load_config(str(proj))
    assert cfg.path is None and cfg.screens == [] and cfg.build_cmd is None


@pytest.mark.skipif(sys.version_info < (3, 11), reason="needs tomllib")
def test_minimal_reader_agrees_with_tomllib():
    import tomllib
    doc = FULL + '\n[extra.nested]\nok = true\nn = 1_000\nf = -2.5e1\ns = "a\\tb\\u00e9"\n' \
                 "m = \"\"\"\nline\"\"\"\nl = 'lit\\n'\n"
    assert config.parse_minimal(doc) == tomllib.loads(doc)


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("bad", [
    "[build\ncmd = 1",                 # unterminated header
    'cmd = "unterminated',             # unterminated string
    "[build]\ncmd = ",                 # missing value
    "[build]\ncmd = 'a'\ncmd = 'b'",   # duplicate key
])
def test_malformed_names_the_file(proj, bad, fallback):
    _write(proj, bad)
    with pytest.raises(config.ConfigError) as ei:
        config.load_config(str(proj), force_fallback=fallback)
    assert "ciqkit.toml" in str(ei.value)


def test_fallback_error_has_line_number(proj):
    _write(proj, "[build]\ncmd = 'a'\n\nbogus line\n")
    with pytest.raises(config.ConfigError, match="line 4"):
        config.load_config(str(proj), force_fallback=True)


@pytest.mark.parametrize("text,msg", [
    ("[build]\ncmd = 3", "build.cmd"),
    ("[[screens]]\nname = 'a/b'", "plain file name"),
    ("[[screens]]\nname = 'a'\n[[screens]]\nname = 'a'", "duplicate"),
    ("[[screens]]\nname = 'a'\nsteps = [1]", "steps"),
    ("[[screens]]\nname = 'a'\nseed = ['NOEQUALS']", "NAME=VALUE"),
    ("[[screens]]\nanchor = 'x'", "name"),
])
def test_type_errors(proj, text, msg):
    _write(proj, text)
    with pytest.raises(config.ConfigError, match=msg):
        config.load_config(str(proj))


def test_unknown_keys_warn_not_fail(proj, caplog):
    _write(proj, "[build]\ncmd='x'\nfoo=1\n[wat]\na=1\n[[screens]]\nname='a'\nbar=2\n")
    with caplog.at_level(logging.WARNING, logger="ciqkit.config"):
        cfg = config.load_config(str(proj))
    assert cfg.build_cmd == "x"
    text = caplog.text
    assert "build.foo" in text and "wat" in text and "screens[0].bar" in text


# --- precedence --------------------------------------------------------------

@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("CIQKIT_BUILD_CMD", raising=False)


def test_build_precedence(proj, clean_env, monkeypatch):
    (proj / "scripts").mkdir()
    (proj / "scripts" / "build.sh").write_text("")
    # script beats built-in
    assert build.resolve_build(str(proj), "d") == (None, build.PROJECT_BUILD_CMD)
    # toml beats script
    _write(proj, "[build]\ncmd='toml {device}'")
    assert build.resolve_build(str(proj), "d") == (None, "toml {device}")
    assert build.build_route(str(proj)).startswith("toml")
    # env beats toml
    monkeypatch.setenv("CIQKIT_BUILD_CMD", "env {device}")
    assert build.resolve_build(str(proj), "d") == (None, "env {device}")
    assert build.build_route(str(proj)).startswith("env")
    # explicit beats env
    assert build.resolve_build(str(proj), "d", build_cmd="cli") == (None, "cli")


def test_test_cmd_used_only_for_tests(proj, clean_env):
    _write(proj, "[build]\ncmd='norm'\ntest_cmd='tst {device}'")
    assert build.resolve_build(str(proj), "d") == (None, "norm")
    assert build.resolve_build(str(proj), "d", tests=True) == (None, "tst {device}")
    assert build.resolve_build(str(proj), "d", tests=True, build_cmd="x") == (None, "x")


def test_device_precedence(proj, monkeypatch, tmp_path):
    devs = tmp_path / "devs"
    (devs / "venu3").mkdir(parents=True)
    monkeypatch.setattr(build, "sdk_devices_dir", lambda: str(devs))
    assert build.resolve_device(str(proj)) == "venu3"          # manifest default
    _write(proj, "[sim]\ndevice='fenix7'")
    assert build.resolve_device(str(proj)) == "fenix7"         # toml
    assert build.resolve_device(str(proj), "epix2") == "epix2"  # flag
    assert mcp_server._device({}, str(proj)) == "fenix7"
    assert mcp_server._device({"device": "epix2"}, str(proj)) == "epix2"


def test_malformed_toml_fails_resolve_build_loudly(proj, clean_env):
    _write(proj, "[build\n")
    with pytest.raises(config.ConfigError):
        build.resolve_build(str(proj), "d")


def test_doctor_validates_toml(proj, clean_env):
    from ciqkit import doctor
    _write(proj, FULL)
    c = {x["name"]: x for x in doctor.project_checks(str(proj))}
    assert c["ciqkit.toml"]["status"] == "warn"      # fenix7 not in manifest
    assert "2 screens" in c["ciqkit.toml"]["detail"]
    assert c["build"]["detail"].startswith("toml")
    _write(proj, "[build\n")
    c = {x["name"]: x for x in doctor.project_checks(str(proj))}
    assert c["ciqkit.toml"]["status"] == "fail"


# --- shots dispatch ----------------------------------------------------------

class FakeClient:
    jobs = []

    def __init__(self, *a, **k):
        pass

    def submit_wait(self, job, timeout_s=0):
        FakeClient.jobs.append(job)
        name = job.label.split(" ", 1)[1]
        if name == "bad":
            return {"ok": False, "job": {"id": "j", "error": "boom", "results": []}}
        return {"ok": True, "job": {"id": "j" + name, "results": [
            {"kind": ChunkKind.SHOT, "result": {"ok": True, "path": f"/o/{name}.png",
                                                "anchor": None}}]}}


@pytest.fixture
def fake(monkeypatch):
    FakeClient.jobs = []
    monkeypatch.setattr(client, "Client", FakeClient)
    monkeypatch.setattr(mcp_server, "Client", FakeClient)
    monkeypatch.setattr(build, "sdk_devices_dir", lambda: "/nonexistent")
    return FakeClient


def _kinds(job):
    return [c.kind for c in job.chunks]


def test_shots_all_submits_one_job_per_screen(proj, fake, capsys):
    _write(proj, FULL)
    rc = client.main(["shots", "--all", "--project", str(proj), "--out-dir", "/o"])
    assert rc == 0
    home, st = fake.jobs
    assert [j.label for j in fake.jobs] == ["shot home", "shot settings"]
    assert all(j.device == "fenix7" for j in fake.jobs)      # [sim] device
    assert _kinds(home) == [ChunkKind.LOAD, ChunkKind.SHOT]
    assert home.chunks[0].args["anchor"] == ["12:00"]
    assert home.chunks[0].args["force"] is True
    assert home.chunks[1].args["out_dir"] == "/o"
    assert _kinds(st) == [ChunkKind.SEED, ChunkKind.LOAD, ChunkKind.REACH,
                          ChunkKind.SHOT, ChunkKind.RESTORE]
    assert st.chunks[0].args["consts"] == {"SEED_DEMO": "true", "SEED_PAGE": "2"}
    # anchor moves from LOAD to REACH when there are steps
    assert "anchor" not in st.chunks[1].args
    assert st.chunks[2].args["anchor"] == ["Settings", "Units"]
    assert st.chunks[2].args["steps"][0]["kind"] == "tap"
    assert st.chunks[1].args["storage"] == str(proj / "fixtures" / "full")
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["screen"] for r in rows] == ["home", "settings"]
    assert rows[0]["path"] == "/o/home.png" and rows[0]["ok"]


def test_shots_screen_subset_and_unknown(proj, fake):
    _write(proj, FULL)
    assert client.main(["shots", "--screen", "settings", "--project", str(proj)]) == 0
    assert [j.label for j in fake.jobs] == ["shot settings"]
    with pytest.raises(SystemExit, match="no screen 'nope'"):
        client.main(["shots", "--screen", "nope", "--project", str(proj)])
    with pytest.raises(SystemExit, match="exactly one"):
        client.main(["shots", "--project", str(proj)])


def test_shots_failure_exits_nonzero_and_continues(proj, fake, capsys):
    _write(proj, "[[screens]]\nname='bad'\n[[screens]]\nname='good'\n")
    rc = client.main(["shots", "--all", "--project", str(proj)])
    assert rc == 1 and len(fake.jobs) == 2
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[0]["ok"] is False and rows[0]["error"] == "boom"
    assert rows[1]["ok"] is True


def test_shots_cli_storage_and_device_override(proj, fake):
    _write(proj, FULL)
    client.main(["shots", "--all", "--project", str(proj), "--device", "epix2",
                 "--storage", "keep"])
    assert all(j.device == "epix2" for j in fake.jobs)
    assert all(j.chunks[[c.kind for c in j.chunks].index(ChunkKind.LOAD)]
               .args["storage"] == "keep" for j in fake.jobs)


def test_shots_seed_with_prg_refused(proj, fake):
    _write(proj, FULL)
    with pytest.raises(SystemExit, match="--seed and --prg"):
        client.main(["shots", "--all", "--prg", "bin/a.prg", "--project", str(proj)])
    assert fake.jobs == []


def test_shot_cli_job_unchanged(proj, fake):
    client.main(["shot", "x", "--anchor", "A", "--seed", "K=1", "--project", str(proj)])
    (job,) = fake.jobs
    assert _kinds(job) == [ChunkKind.SEED, ChunkKind.LOAD, ChunkKind.SHOT,
                           ChunkKind.RESTORE]
    assert job.chunks[1].args["anchor"] == ["A"]


def test_malformed_toml_is_a_clean_cli_error(proj, fake):
    _write(proj, "[build\n")
    with pytest.raises(SystemExit, match="ciqkit.toml"):
        client.main(["shots", "--all", "--project", str(proj)])


def test_mcp_shots_tool(proj, fake):
    _write(proj, FULL)
    out = json.loads(mcp_server._call_tool(
        "ciqsim_shots", {"project": str(proj), "screens": ["home"]}))
    assert out["ok"] and out["screens"][0]["path"] == "/o/home.png"
    assert [j.label for j in fake.jobs] == ["shot home"]
    assert "ciqsim_shots" in [t["name"] for t in mcp_server.TOOLS]


def test_session_accepts_toml_test_cmd(proj, clean_env):
    _write(proj, "[build]\ncmd='norm'\ntest_cmd='tst {device}'")
    # the guard in SimSession.build compares against the toml test_cmd
    _, tmpl = build.resolve_build(str(proj), "d", tests=True)
    assert tmpl == build.toml_test_cmd(str(proj))
