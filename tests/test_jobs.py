"""Job builders: one definition of each job, shared by the CLI and the MCP server."""
import json
import os

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


# ── chunk budgets: one owner (protocol.CHUNK_BUDGETS) ────────────────────────
def test_from_dict_without_budgets_uses_the_kind_table():
    c = proto.Chunk.from_dict({"kind": "load", "args": {"project": "/p"}})
    assert (c.expected_s, c.max_s) == (40.0, 300.0)
    c = proto.Chunk.from_dict({"kind": "test"})
    assert (c.expected_s, c.max_s) == (120.0, 900.0)


def test_from_dict_keeps_explicit_budgets():
    c = proto.Chunk.from_dict({"kind": "load", "expected_s": 3, "max_s": 7})
    assert (c.expected_s, c.max_s) == (3.0, 7.0)


def test_unknown_kind_gets_fallback_budget():
    c = proto.Chunk.from_dict({"kind": "nonsense"})
    assert (c.expected_s, c.max_s) == (15.0, 120.0)


def test_list_kind_does_not_raise_but_is_refused_by_validate_job():
    c = proto.Chunk.from_dict({"kind": ["load"]})
    assert (c.expected_s, c.max_s) == (15.0, 120.0)
    job = proto.Job(app="a", chunks=[c])
    assert proto.validate_job(job)


def test_default_device_is_one_constant():
    assert proto.DEFAULT_DEVICE == "venu3"
    assert proto.Job(app="a", chunks=[]).device == proto.DEFAULT_DEVICE
    assert proto.Job.from_dict({"app": "a"}).device == proto.DEFAULT_DEVICE


def test_builders_keep_their_pinned_budgets(project):
    load = jobs.load_chunk(device="venu3", project=str(project))
    assert (load.expected_s, load.max_s) == (40, 300)
    assert (jobs.SHOT_EXPECTED_S, jobs.SHOT_MAX_S) == (15.0, 120.0)
    restore = jobs._restore_chunk()
    assert (restore.expected_s, restore.max_s) == (1, 30)
    ensure = jobs.op_chunks([])[0]
    assert (ensure.kind, ensure.expected_s, ensure.max_s) == ("ensure", 5.0, 60.0)
    build = jobs.test_chunks(project=str(project), device="venu3")[0]
    assert (build.expected_s, build.max_s) == (120, 900)
    prg = jobs.test_chunks(project=str(project), device="venu3", prg="a.prg")[0]
    assert prg.max_s == 600


def test_project_dir_expands_tilde_and_checks_the_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # expanduser reads this on Windows
    app = tmp_path / "app"
    app.mkdir()
    with pytest.raises(jobs.JobError, match="manifest.xml"):
        jobs.project_dir(os.path.join("~", "app"))
    (app / "manifest.xml").write_bytes(b"<iq:manifest/>")
    assert jobs.project_dir(os.path.join("~", "app")) == os.path.abspath(str(app))
    with pytest.raises(jobs.JobError, match="does not exist"):
        jobs.project_dir(os.path.join("~", "nope"))


def test_app_name_is_the_directory_basename():
    assert jobs.app_name(os.path.join("x", "my-app") + os.sep) == "my-app"


def test_priority_maps_names_and_refuses_unknown_ones():
    assert jobs.priority(None) == proto.Priority.NORMAL
    assert jobs.priority("batch") == proto.Priority.BATCH
    assert jobs.priority("batch", urgent=True) == proto.Priority.URGENT
    with pytest.raises(jobs.JobError, match="bogus"):
        jobs.priority("bogus")


# ── one verdict: the CLI exit codes and MCP summaries, pinned per case ───────
# The CLI's exit code and the MCP tools' `ok`/`error` used to be decided by
# separate helpers in client.py and mcp_server.py. They now share one verdict;
# this table is what both front-ends answered before the move, so the move
# cannot change an exit code or a summary an agent reads.

def _vjob(results, ok=True, state="done", error=None, top=None):
    r = {"ok": ok, "job": {"id": "j1", "state": state, "error": error,
                           "results": results}}
    r.update(top or {})
    return r


def _vstep(kind, **res):
    return {"kind": kind, "result": res}


_TIMEOUT = {"error": "wait timeout"}
VERDICT_CASES = {
    "done-empty": _vjob([]),
    "failed": _vjob([], ok=False, state="failed", error="boom"),
    "wait-timeout": _vjob([], ok=False, state="running", top=_TIMEOUT),
    "wait-timeout-coded": _vjob([], ok=False, state="running",
                                top={**_TIMEOUT, "code": proto.E_WAIT_TIMEOUT}),
    "wait-timeout-finished": _vjob([], ok=False, state="failed", top=_TIMEOUT),
    "shot-anchor-true": _vjob([_vstep("load", ok=True),
                               _vstep("shot", ok=True, path="p.png", anchor=True)]),
    "shot-anchor-false": _vjob([_vstep("shot", ok=True, path="p.png", anchor=False)]),
    "shot-anchor-none": _vjob([_vstep("shot", ok=True, path="p.png", anchor=None)]),
    "shot-step-not-ok": _vjob([_vstep("shot", ok=False, reason="no marker")]),
    "reach-ok": _vjob([_vstep("load", ok=True),
                       _vstep("reach", ok=True, screen="s", anchor=True, steps=[])]),
    "reach-anchor-false": _vjob([_vstep("reach", ok=False, screen="s", anchor=False,
                                        steps=[])]),
    "reach-not-ok": _vjob([_vstep("reach", ok=False, screen="s", anchor=None, steps=[],
                                  error="step 0 (tap) did not change")]),
    "reach-not-ok-no-error": _vjob([_vstep("reach", ok=False, screen="s", anchor=None,
                                           steps=[])]),
    "reach-failed-job": _vjob([_vstep("reach", ok=False, screen="s", anchor=False)],
                              ok=False, state="failed", error="later chunk died"),
    "test-ok": _vjob([_vstep("test", ok=True, passed=3, failed=0, errors=0)]),
    "test-reason": _vjob([_vstep("test", ok=False, reason="simulator-unreachable")]),
    "test-counts": _vjob([_vstep("test", ok=False, passed=1, failed=2, errors=0)]),
    "test-no-ok": _vjob([_vstep("test", passed=1)]),
    "load-not-ok": _vjob([_vstep("load", ok=False),
                          _vstep("reach", ok=True, screen="s", anchor=None, steps=[])]),
    "null-result": _vjob([{"kind": "shot", "result": None}]),
}

_NAV_ANCHOR = "ran the steps but the target screen 's' did not show the expected text"
_TOUT = ("wait timeout", True)
# case: CLI exit codes (shot, shot-on, wait, record, nav, test, phone),
#       MCP shot/record/test (ok, error, still_running), MCP nav (ok, error),
#       the per-screen row of `shots` (ok, error)
VERDICT_TABLE = {
    "done-empty": ((0, 0, 0, 0, 0, 0, 0), (True, None, None), (True, None),
                   (True, None)),
    "failed": ((1, 1, 1, 1, 1, 1, 1), (False, "boom", None), (False, "boom"),
               (False, "boom")),
    "wait-timeout": ((1, 1, 1, 1, 1, 1, 1), (False, *_TOUT), (False, "wait timeout"),
                     (False, "wait timeout")),
    "wait-timeout-coded": ((1, 1, 1, 1, 1, 1, 1), (False, *_TOUT),
                           (False, "wait timeout"), (False, "wait timeout")),
    "wait-timeout-finished": ((1, 1, 1, 1, 1, 1, 1), (False, "wait timeout", None),
                              (False, "wait timeout"), (False, "wait timeout")),
    "shot-anchor-true": ((0, 0, 0, 0, 0, 0, 0), (True, None, None), (True, None),
                         (True, None)),
    "shot-anchor-false": ((3, 3, 3, 0, 0, 0, 0), (True, None, None), (True, None),
                          (False, "expected anchor text was not on screen")),
    "shot-anchor-none": ((0, 0, 0, 0, 0, 0, 0), (True, None, None), (True, None),
                         (True, None)),
    "shot-step-not-ok": ((0, 0, 0, 0, 0, 0, 0), (False, None, None), (False, None),
                         (True, None)),
    "reach-ok": ((0, 0, 0, 0, 0, 0, 0), (True, None, None), (True, None), (True, None)),
    "reach-anchor-false": ((0, 0, 0, 0, 3, 0, 0), (False, None, None),
                           (False, _NAV_ANCHOR),
                           (False, "steps ran but the screen did not show the "
                                   "expected text")),
    "reach-not-ok": ((0, 0, 0, 0, 1, 0, 0), (False, None, None),
                     (False, "step 0 (tap) did not change"),
                     (False, "step 0 (tap) did not change")),
    "reach-not-ok-no-error": ((0, 0, 0, 0, 1, 0, 0), (False, None, None),
                              (False, "nav did not reach the screen"),
                              (False, "steps did not reach the screen")),
    "reach-failed-job": ((1, 1, 1, 1, 1, 1, 1), (False, "later chunk died", None),
                         (False, _NAV_ANCHOR), (False, "later chunk died")),
    "test-ok": ((0, 0, 0, 0, 0, 0, 0), (True, None, None), (True, None), (True, None)),
    "test-reason": ((0, 0, 0, 0, 0, 1, 0), (False, None, None), (False, None),
                    (True, None)),
    "test-counts": ((0, 0, 0, 0, 0, 1, 0), (False, None, None), (False, None),
                    (True, None)),
    "test-no-ok": ((0, 0, 0, 0, 0, 1, 0), (True, None, None), (True, None),
                   (True, None)),
    "load-not-ok": ((0, 0, 0, 0, 0, 0, 0), (False, None, None), (False, None),
                    (True, None)),
    "null-result": ((0, 0, 0, 0, 0, 0, 0), (True, None, None), (True, None),
                    (True, None)),
}
# What the CLI prints to stderr beside a non-zero nav/test exit.
_CLI_STDERR = {
    ("reach-anchor-false", "nav"): f"error: {_NAV_ANCHOR}",
    ("reach-not-ok", "nav"): "error: step 0 (tap) did not change",
    ("reach-not-ok-no-error", "nav"): "error: nav did not reach the screen",
    ("test-reason", "test"): "error: tests did not pass (simulator-unreachable)",
    ("test-counts", "test"): "error: tests did not pass (passed=1 failed=2 errors=0)",
    ("test-no-ok", "test"): "error: tests did not pass (passed=1 failed=None errors=None)",
}


def test_the_verdict_table_covers_every_case():
    assert set(VERDICT_TABLE) == set(VERDICT_CASES)


@pytest.mark.parametrize("case", sorted(VERDICT_CASES))
def test_cli_exit_codes_per_command(case, project, monkeypatch, capsys):
    monkeypatch.setattr("ciqkit.build.resolve_device", lambda proj, d: d or "venu3")
    r = VERDICT_CASES[case]
    monkeypatch.setattr(sim_client.Client, "submit_wait",
                        lambda self, job, timeout_s=None: r)
    monkeypatch.setattr(sim_client.Client, "wait",
                        lambda self, jid, timeout_s=600.0: r)
    p = ["--project", str(project), "--device", "venu3", "--prg", "a.prg"]
    argv = {"shot": ["shot", "home", *p], "shot-on": ["shot-on", "--marker", "T=n", *p],
            "wait": ["wait", "j1"], "record": ["record", "r", *p],
            "nav": ["nav", "--to", "s", *p], "test": ["test", *p],
            "phone": ["phone", "connected"]}
    for cmd, want in zip(argv, VERDICT_TABLE[case][0]):
        assert sim_client.main(argv[cmd]) == want, cmd
        err = capsys.readouterr().err
        if (case, cmd) in _CLI_STDERR:
            assert err.strip() == _CLI_STDERR[(case, cmd)], cmd
        elif case.startswith("wait-timeout") and cmd != "phone":
            assert "ciqkit sim wait j1" in err, cmd
        else:
            assert err == "", cmd


@pytest.mark.parametrize("case", sorted(VERDICT_CASES))
def test_mcp_summaries_per_tool(case, project, monkeypatch):
    r = VERDICT_CASES[case]
    monkeypatch.setattr(mcp_server.Client, "submit_wait",
                        lambda self, job, timeout_s=None: r)
    _, job_want, nav_want, _ = VERDICT_TABLE[case]
    base = {"project": str(project), "device": "venu3", "prg": "a.prg"}
    for tool, extra in (("ciqsim_shot", {"name": "h"}), ("ciqsim_record", {"name": "r"}),
                        ("ciqsim_test", {})):
        out = json.loads(mcp_server._call_tool(tool, {**base, **extra}))
        assert (out["ok"], out["error"], out.get("still_running")) == job_want, tool
        assert out["job_id"] == "j1"
        assert out.get("steps", []) == [{s["kind"]: s["result"]}
                                        for s in r["job"]["results"]]
        if job_want[2]:
            assert out["state"] == "running" and "do not resubmit" in out["note"]
        else:
            assert not {"state", "note"} & set(out)
    nav = json.loads(mcp_server._call_tool("ciqsim_nav", {**base, "to": "s"}))
    assert (nav["ok"], nav["error"]) == nav_want


@pytest.mark.parametrize("case", sorted(VERDICT_CASES))
def test_the_shots_row_per_case(case):
    row = sim_client.screen_summary("s", VERDICT_CASES[case])
    ok, err = VERDICT_TABLE[case][3]
    assert row["ok"] is ok and row.get("error") == err
    assert row["job_id"] == "j1"


@pytest.mark.parametrize("case", sorted(VERDICT_CASES))
def test_verdict_table(case):
    """The same table, straight from jobs.verdict: one rule per command."""
    r = VERDICT_CASES[case]
    (shot, _, _, record, nav, test, op), job, nav_mcp, screen = VERDICT_TABLE[case]
    v = {c: jobs.verdict(r, c) for c in jobs.VERDICT_COMMANDS}
    assert [v[c].exit_code for c in (jobs.SHOT, jobs.RECORD, jobs.NAV, jobs.TEST,
                                     jobs.OP)] == [shot, record, nav, test, op]
    assert (v[jobs.JOB].ok, v[jobs.JOB].reason, v[jobs.JOB].still_running or None) == job
    assert (v[jobs.NAV].ok, v[jobs.NAV].reason) == nav_mcp
    assert (v[jobs.SCREEN].ok, v[jobs.SCREEN].reason) == screen
    for c in (jobs.SHOT, jobs.RECORD, jobs.TEST, jobs.OP, jobs.JOB, jobs.SCREEN):
        assert v[c].ok == (v[c].exit_code == 0), c


def test_verdict_refuses_an_unknown_command():
    with pytest.raises(ValueError):
        jobs.verdict({"ok": True}, "shots")


def test_op_exits_1_when_a_nested_reach_step_failed():
    """reach nests `steps` under each entry's "result"; a failed step there must
    fail `phone`/`close-aux`, not exit 0 on an unverified action."""
    def res(ok):
        return {"ok": True, "job": {"state": "done", "results": [
            {"kind": "reach", "result": {"ok": True, "steps": [{"ok": True}, {"ok": ok}]}}]}}
    assert jobs.verdict(res(True), jobs.OP).exit_code == 0
    assert jobs.verdict(res(False), jobs.OP).exit_code == 1
