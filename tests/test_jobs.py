"""Job builders: one definition of each job, shared by the CLI and the MCP server."""
import pytest

from ciqkit.sim import client as sim_client
from ciqkit.sim import jobs, mcp_server
from ciqkit.sim import protocol as proto


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "my-app"
    p.mkdir()
    (p / "manifest.xml").write_text("<iq:manifest/>")
    return p


def _valid(chunks, project):
    job = jobs.make_job(app="a", device="venu3", chunks=chunks)
    assert proto.validate_job(job) == []
    return job


def test_every_builder_output_validates(project):
    kw = dict(project=str(project), device="venu3")
    _valid(jobs.screen_chunks(name="home", **kw), project)
    _valid(jobs.screen_chunks(name="home", anchor="hi", out_dir="o",
                              steps=[{"tap": [0.5, 0.5]}], storage="fresh",
                              force=True, crop=False, frame=False,
                              **{**kw, "prg": "b/A.prg"}), project)
    _valid(jobs.screen_chunks(name="home", seed={"X": "1"}, **kw), project)
    _valid(jobs.record_chunks(name="clip", duration_s=5, interval_ms=200,
                              playback_ms=500, seed={"X": "1"}, **kw), project)
    _valid(jobs.nav_chunks(to="menu", steps=[{"tap": [0.1, 0.2]}], **kw), project)
    _valid(jobs.nav_chunks(to="menu", steps=[], **kw), project)
    _valid(jobs.test_chunks(**kw), project)
    _valid(jobs.test_chunks(prg="t.prg", max_s=99, **kw), project)
    _valid(jobs.screen_chunks(name="home", prg="b/A.prg", **kw), project)


def test_make_job_refuses_an_invalid_job_with_every_reason():
    with pytest.raises(ValueError) as exc:
        jobs.make_job(app="a", device="venu3", priority=99, chunks=[])
    msg = str(exc.value)
    assert "no chunks" in msg and "priority" in msg


def test_make_job_refuses_a_chunk_missing_its_name():
    bad = proto.Chunk(proto.ChunkKind.SHOT, {})
    with pytest.raises(ValueError, match="missing name"):
        jobs.make_job(app="a", device="venu3", chunks=[bad])


def test_seed_with_prg_is_refused_by_the_builders(project):
    kw = dict(project=str(project), device="venu3", prg="a.prg", seed={"X": "1"})
    with pytest.raises(ValueError, match="UNSEEDED"):
        jobs.screen_chunks(name="x", **kw)
    with pytest.raises(ValueError, match="UNSEEDED"):
        jobs.record_chunks(name="x", duration_s=5, interval_ms=200,
                           playback_ms=500, **kw)


def test_shot_settle_is_the_shared_default(project):
    shot = jobs.screen_chunks(project=str(project), device="venu3", name="h")[-1]
    assert shot.args["settle_s"] == jobs.DEFAULT_SHOT_SETTLE_S == 0.35


def _cli_chunks(monkeypatch, argv):
    captured = {}

    def fake(self, job, timeout_s=0):
        captured["job"] = job
        return {"ok": True, "job": {"id": "x", "results": []}}

    monkeypatch.setattr(sim_client.Client, "submit_wait", fake)
    sim_client.main(argv)
    return captured["job"].to_dict()["chunks"]


def _mcp_chunks(monkeypatch, args):
    captured = {}

    def fake(self, job, timeout_s=0):
        captured["job"] = job
        return {"ok": True, "job": {"id": "x", "results": []}}

    monkeypatch.setattr(mcp_server.Client, "submit_wait", fake)
    mcp_server._call_tool("ciqsim_shot", args)
    return captured["job"].to_dict()["chunks"]


@pytest.mark.parametrize("extra_cli,extra_mcp", [
    ([], {}),
    (["--prg", "bin/App.prg"], {"prg": "bin/App.prg"}),
    (["--prg", "bin/App.prg", "--force"], {"prg": "bin/App.prg", "force": True}),
    # --anchor is repeatable on the CLI, so it ships a list; the MCP schema
    # takes one string. Same builder, different (intended) input shape.
    (["--anchor", "Hello"], {"anchor": ["Hello"]}),
    (["--native"], {"native": True}),
    (["--out-dir", "shots"], {"out_dir": "shots"}),
])
def test_mcp_shot_and_cli_shot_build_identical_chunks(
        project, monkeypatch, extra_cli, extra_mcp):
    monkeypatch.setattr("ciqkit.build.resolve_device", lambda proj, d: d or "venu3")
    cli = _cli_chunks(monkeypatch, ["shot", "home", "--project", str(project),
                                    "--device", "venu3", *extra_cli])
    mcp = _mcp_chunks(monkeypatch, {"project": str(project), "name": "home",
                                    "device": "venu3", **extra_mcp})
    assert cli == mcp


def test_the_wait_covers_the_jobs_own_worst_case(project):
    """The front-ends used to wait a hand-picked 360/400 s, below the broker's
    own ceiling for a shot (LOAD 300 + SHOT 120), so a slow but healthy build
    came back as a timeout while the job kept running."""
    job = jobs.make_job(app="a", device="venu3", chunks=jobs.screen_chunks(
        project=str(project), device="venu3", name="h", seed={"X": "1"},
        steps=[{"tap": [0.5, 0.5]}]))
    assert jobs.wait_budget(job) >= sum(c.max_s for c in job.chunks) + jobs.QUEUE_SLACK_S


def test_submit_wait_derives_its_timeout_from_the_job(project, monkeypatch):
    seen = {}
    monkeypatch.setattr(sim_client.Client, "submit", lambda self, job: "j1")
    monkeypatch.setattr(sim_client.Client, "wait",
                        lambda self, jid, timeout_s: seen.setdefault("t", timeout_s) and {})
    job = jobs.make_job(app="a", device="venu3", chunks=jobs.test_chunks(
        project=str(project), device="venu3"))
    c = sim_client.Client(autospawn=False)
    c.submit_wait(job)
    assert seen["t"] == jobs.wait_budget(job)
    seen.clear()
    c.submit_wait(job, timeout_s=42)       # the CLI's --max still overrides
    assert seen["t"] == 42


def test_both_front_ends_leave_the_wait_to_the_job_budget(project, monkeypatch):
    monkeypatch.setattr("ciqkit.build.resolve_device", lambda proj, d: d or "venu3")
    timeouts = []

    def fake(self, job, timeout_s=None):
        timeouts.append(timeout_s)
        return {"ok": True, "job": {"id": "x", "results": []}}

    monkeypatch.setattr(sim_client.Client, "submit_wait", fake)
    monkeypatch.setattr(mcp_server.Client, "submit_wait", fake)
    sim_client.main(["shot", "home", "--project", str(project), "--device", "venu3"])
    mcp_server._call_tool("ciqsim_shot", {"project": str(project), "name": "home",
                                          "device": "venu3"})
    assert timeouts == [None, None]


def test_an_mcp_wait_timeout_says_the_job_is_still_running():
    """It used to come back as {"ok": false, "error": null}: an agent read a
    failed job and resubmitted one that was still holding the simulator."""
    r = {"ok": False, "error": "wait timeout",
         "job": {"id": "j1", "state": "running", "error": None, "results": []}}
    out = mcp_server._result_summary(r)
    assert out["ok"] is False and out["error"] == "wait timeout"
    assert out["still_running"] is True and out["job_id"] == "j1"
    done = mcp_server._result_summary(
        {"ok": False, "job": {"id": "j2", "state": "failed", "error": "boom"}})
    assert done["error"] == "boom" and "still_running" not in done
