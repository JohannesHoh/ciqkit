"""CLI surface: help, argument validation, and the guards that stop a
mistyped command from doing something surprising.

None of this touches the simulator.
"""

import sys
import json

import pytest

from ciqkit import cli
from ciqkit.qa import icons
from ciqkit.sim import client, jobs


def test_bare_invocation_lists_commands(capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    for cmd in ("sim", "layout", "shot-score", "icon", "lease", "doctor"):
        assert cmd in out


def test_unknown_command_fails_with_usage(capsys):
    assert cli.main(["nope"]) == 2
    assert "unknown command" in capsys.readouterr().err


def test_version(capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip().startswith("ciqkit ")


@pytest.fixture
def project(tmp_path):
    """A directory that looks enough like a Connect IQ project."""
    p = tmp_path / "my-app"
    (p / "resources" / "drawables").mkdir(parents=True)
    (p / "manifest.xml").write_text("<iq:manifest/>")
    return p


def test_sim_refuses_a_directory_that_is_not_a_project(tmp_path):
    """Better to fail here than to surface later as a confusing build error."""
    with pytest.raises(SystemExit) as exc:
        client.main(["shot", "home", "--project", str(tmp_path)])
    assert "manifest.xml" in str(exc.value)


def test_sim_refuses_a_missing_directory(tmp_path):
    with pytest.raises(SystemExit) as exc:
        client.main(["shot", "home", "--project", str(tmp_path / "nope")])
    assert "does not exist" in str(exc.value)


def test_sim_nav_rejects_malformed_steps(project):
    with pytest.raises(SystemExit) as exc:
        client.main(["nav", "--to", "x", "--steps", "{not json",
                     "--project", str(project)])
    assert "not valid JSON" in str(exc.value)


def test_sim_nav_rejects_non_list_steps(project):
    with pytest.raises(SystemExit) as exc:
        client.main(["nav", "--to", "x", "--steps", '{"kind":"tap"}',
                     "--project", str(project)])
    assert "JSON list" in str(exc.value)


@pytest.mark.parametrize("bad", ["NOEQUALS", "=novalue"])
def test_seed_rejects_malformed_assignments(bad):
    with pytest.raises(SystemExit) as exc:
        client._parse_kv([bad], "--set")
    assert "NAME=VALUE" in str(exc.value)


def test_seed_parses_assignments():
    assert client._parse_kv(["A=1", " B = two "], "--set") == {"A": "1", "B": "two"}


def test_project_name_is_the_directory_name(project):
    assert client._project_name(str(project)) == "my-app"


# --- icon checks ------------------------------------------------------------

def _png(path, colour):
    Image = pytest.importorskip("PIL.Image", reason="Pillow not installed")
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (70, 70), colour).save(path)


def test_duplicate_icon_is_detected(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _png(icons.icon_path(a), (10, 20, 30, 255))
    _png(icons.icon_path(b), (10, 20, 30, 255))
    assert [p.name for p in icons.find_duplicates(a, [b])] == ["b"]


def test_distinct_icon_passes(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _png(icons.icon_path(a), (10, 20, 30, 255))
    _png(icons.icon_path(b), (200, 0, 0, 255))
    assert icons.find_duplicates(a, [b]) == []


def test_a_project_never_collides_with_itself(tmp_path):
    a = tmp_path / "a"
    _png(icons.icon_path(a), (10, 20, 30, 255))
    assert icons.find_duplicates(a, [a]) == []


def test_missing_icon_is_not_this_checks_problem(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    _png(icons.icon_path(b), (10, 20, 30, 255))
    assert icons.find_duplicates(a, [b]) == []


def test_unique_check_exits_nonzero_on_a_copied_icon(tmp_path, capsys):
    a, b = tmp_path / "a", tmp_path / "b"
    _png(icons.icon_path(a), (10, 20, 30, 255))
    _png(icons.icon_path(b), (10, 20, 30, 255))
    assert icons.main(["unique", str(a), str(b)]) == 1
    assert "byte-identical" in capsys.readouterr().err


def test_beta_ribbon_preserves_dimensions_and_changes_pixels(tmp_path):
    Image = pytest.importorskip("PIL.Image", reason="Pillow not installed")
    src, dst = tmp_path / "icon.png", tmp_path / "beta.png"
    _png(src, (10, 20, 30, 255))
    icons.beta_ribbon(src, dst)
    before, after = Image.open(src), Image.open(dst)
    assert after.size == before.size
    assert after.tobytes() != before.tobytes()


# --- job construction: what actually reaches the broker ---------------------

def _submitted(monkeypatch, argv):
    """Run the CLI with the broker stubbed out and return the Job it built."""
    from ciqkit.sim import client as sim_client
    captured = {}

    def fake_submit_wait(self, job, timeout_s=0):
        captured["job"] = job
        return {"ok": True, "job": {"id": "x", "results": []}}

    monkeypatch.setattr(sim_client.Client, "submit_wait", fake_submit_wait)
    sim_client.main(argv)
    return captured["job"]


def _chunk(job, kind):
    return next(c for c in job.chunks if c.kind == kind)


def test_force_reaches_the_load_chunk(project, monkeypatch):
    """Without this the warm-load shortcut re-shoots the previous build."""
    job = _submitted(monkeypatch, ["shot", "home", "--project", str(project),
                                   "--prg", "bin/App.prg", "--force"])
    assert _chunk(job, "load").args["force"] is True


def test_force_is_absent_by_default(project, monkeypatch):
    job = _submitted(monkeypatch, ["shot", "home", "--project", str(project),
                                   "--prg", "bin/App.prg"])
    assert "force" not in _chunk(job, "load").args


def test_a_relative_prg_is_resolved_against_the_project(project, monkeypatch):
    job = _submitted(monkeypatch, ["shot", "home", "--project", str(project),
                                   "--prg", "bin/App.prg"])
    assert _chunk(job, "load").args["prg"] == str(project / "bin" / "App.prg")


def test_omitting_prg_asks_the_broker_to_build(project, monkeypatch):
    job = _submitted(monkeypatch, ["shot", "home", "--project", str(project)])
    args = _chunk(job, "load").args
    assert args["app_dir"] == str(project) and "prg" not in args


def test_out_dir_reaches_the_shot_chunk(project, monkeypatch, tmp_path):
    job = _submitted(monkeypatch, ["shot", "home", "--project", str(project),
                                   "--prg", "bin/App.prg",
                                   "--out-dir", str(tmp_path)])
    assert _chunk(job, "shot").args["out_dir"] == str(tmp_path)


def test_native_turns_off_framing(project, monkeypatch):
    job = _submitted(monkeypatch, ["shot", "home", "--project", str(project),
                                   "--prg", "bin/App.prg", "--native"])
    assert _chunk(job, "shot").args["frame"] is False


def test_urgent_raises_the_priority(project, monkeypatch):
    from ciqkit.sim.protocol import Priority
    job = _submitted(monkeypatch, ["shot", "home", "--project", str(project),
                                   "--prg", "bin/App.prg", "--urgent"])
    assert job.priority == Priority.URGENT


def test_tests_default_to_batch_so_they_yield_to_interactive_work(project, monkeypatch):
    from ciqkit.sim.protocol import Priority
    job = _submitted(monkeypatch, ["test", "--project", str(project),
                                   "--prg", "bin/App-test.prg"])
    assert job.priority == Priority.BATCH


def test_a_seeded_recording_restores_afterwards(project, monkeypatch):
    """The seed patch must be undone in the same job, not left behind."""
    job = _submitted(monkeypatch, ["record", "clip", "--project", str(project),
                                   "--seed", "SEED_DEMO=true"])
    kinds = [c.kind for c in job.chunks]
    assert kinds.index("seed") < kinds.index("record") < kinds.index("restore")
    assert _chunk(job, "seed").args["consts"] == {"SEED_DEMO": "true"}


# --- anchors: the "assert the screen says X" promise -------------------------

def test_the_anchor_is_also_used_to_verify_the_load(project, monkeypatch):
    """Matching the expected text proves the RIGHT screen is up, not merely
    that something changed. Without this the anchor only ran after capture."""
    job = _submitted(monkeypatch, ["shot", "home", "--project", str(project),
                                   "--prg", "bin/App.prg", "--anchor", "TOTAL"])
    assert _chunk(job, "load").args["anchor"] == ["TOTAL"]
    assert _chunk(job, "shot").args["anchor"] == ["TOTAL"]


def test_no_anchor_means_no_anchor_on_the_load_chunk(project, monkeypatch):
    job = _submitted(monkeypatch, ["shot", "home", "--project", str(project),
                                   "--prg", "bin/App.prg"])
    assert "anchor" not in _chunk(job, "load").args


def _result(anchor):
    return {"ok": True, "job": {"results": [{"kind": "shot",
                                             "result": {"anchor": anchor}}]}}


def _shot_exit(r):
    return jobs.verdict(r, jobs.SHOT).exit_code


def test_a_matched_anchor_passes():
    assert _shot_exit(_result(True)) == 0


def test_a_missing_anchor_fails():
    assert _shot_exit(_result(False)) == 3


def test_an_unavailable_ocr_backend_does_not_fail_the_command():
    """`None` is "skipped", not "failed" -- otherwise every machine without an
    OCR backend would start failing captures that are perfectly fine."""
    assert _shot_exit(_result(None)) == 0


def test_a_run_with_no_shot_step_passes():
    assert _shot_exit({"ok": True, "job": {"results": []}}) == 0


# --- seeding only means anything if a build follows --------------------------

@pytest.mark.parametrize("argv", [
    ["shot", "home", "--prg", "bin/App.prg", "--seed", "SEED_DEMO=true"],
    ["record", "clip", "--prg", "bin/App.prg", "--seed", "SEED_DEMO=true"],
])
def test_seed_with_a_prebuilt_prg_is_refused(project, argv):
    """--seed patches source for the *build*; --prg skips the build. Together
    they capture the UNSEEDED app and report it verified -- exactly the
    confident-wrong-answer failure this tool exists to prevent."""
    with pytest.raises(SystemExit) as exc:
        client.main(argv + ["--project", str(project)])
    msg = str(exc.value)
    assert "--seed and --prg" in msg and "UNSEEDED" in msg


# --- looking at the broker must not start one --------------------------------

@pytest.fixture
def no_spawn(monkeypatch):
    """Fail loudly if a command tries to start a daemon."""
    def boom(*_a, **_k):
        raise AssertionError("this command must not spawn a broker")
    monkeypatch.setattr(client, "spawn_daemon", boom)


@pytest.mark.parametrize("cmd,expected_rc", [
    (["status"], 0),          # "nothing running" is the answer, not an error
    (["shutdown"], 0),        # already stopped -- idempotent
    (["ping"], 1),            # ping must be able to say NO
    (["cancel", "abc"], 1),
    (["bump", "abc"], 1),
    (["wait", "abc"], 1),
])
def test_read_only_commands_do_not_start_a_broker(no_spawn, short_runtime_dir,
                                                  cmd, expected_rc, capsys):
    """With autospawn, `ping` could never report "not running", and `status`
    would boot a daemon that then idles for ten minutes to say there was
    nothing to see."""
    assert client.main(cmd) == expected_rc
    out = capsys.readouterr()
    assert "no broker is running" in (out.out + out.err)


def test_a_submitting_command_still_autospawns(project, monkeypatch):
    """Only the read-only commands changed; `shot` must still start the broker
    ssh-agent style."""
    seen = {}

    def fake_submit_wait(self, job, timeout_s=0):
        seen["autospawn"] = self.autospawn
        return {"ok": True, "job": {"id": "x", "results": []}}

    monkeypatch.setattr(client.Client, "submit_wait", fake_submit_wait)
    client.main(["shot", "home", "--project", str(project), "--prg", "a.prg"])
    assert seen["autospawn"] is True


# --- a wait timeout is not a cancellation ------------------------------------

def test_a_wait_timeout_says_the_job_is_still_running(project, monkeypatch, capsys):
    """The default wait is shorter than a queued build can take. Without this
    the printed job id is a dead end and the user assumes the job died."""
    monkeypatch.setattr(client.Client, "submit_wait",
                        lambda self, job, timeout_s=0: {
                            "ok": False, "error": "wait timeout",
                            "job": {"id": "j7", "state": "running", "results": []}})
    assert client.main(["shot", "home", "--project", str(project),
                        "--prg", "a.prg"]) == 1
    out = capsys.readouterr()
    import json as _json
    _json.loads(out.out)          # stdout stays parseable JSON
    assert "ciqkit sim wait j7" in out.err


def test_wait_reattaches_to_a_running_job(monkeypatch, capsys):
    calls = {}

    def fake_wait(self, job_id, timeout_s=600.0):
        calls["args"] = (job_id, timeout_s, self.autospawn)
        return {"ok": True, "job": {"id": job_id, "results": [
            {"kind": "shot", "result": {"anchor": True}}]}}

    monkeypatch.setattr(client.Client, "wait", fake_wait)
    assert client.main(["wait", "j7", "--timeout", "12"]) == 0
    assert calls["args"] == ("j7", 12.0, False)
    assert "j7" in capsys.readouterr().out


def test_wait_keeps_the_anchor_contract(monkeypatch):
    """Re-attaching must report a contradicted anchor the same way the original
    command would have -- exit 3, not a cheerful 0."""
    monkeypatch.setattr(client.Client, "wait",
                        lambda self, job_id, timeout_s=600.0: {
                            "ok": True, "job": {"id": job_id, "results": [
                                {"kind": "shot", "result": {"anchor": False}}]}})
    assert client.main(["wait", "j7"]) == 3


def test_a_broker_that_dies_mid_reply_is_not_reported_as_absent(monkeypatch):
    """"No daemon" and "the daemon died mid-reply" are both OSError. Reporting
    the second as the first sends the user hunting for a broker that was there
    a moment ago -- and tells an agent to resubmit work still in flight."""
    def boom(self):
        raise ConnectionResetError("broker went away")

    monkeypatch.setattr(client.Client, "status", boom)
    with pytest.raises(ConnectionResetError):
        client.main(["status"])


@pytest.mark.parametrize("cmd", [["cancel", "gone"], ["bump", "gone"]])
def test_acting_on_a_job_the_broker_does_not_know_exits_nonzero(monkeypatch, cmd,
                                                                capsys):
    """Retention reaping makes this routine for anything older than 15 minutes.
    Exiting 0 while the JSON on stdout says the command failed is worse than
    the failure."""
    # The broker's reply runs through the client's own check, which raises
    # BrokerError on it; main() prints the reply and exits 1.
    reply = {"ok": False, "error": "no such job", "code": "no_such_job"}
    monkeypatch.setattr(client.Client, "_rpc", lambda self, req, read_timeout=None: reply)
    assert client.main(cmd) == 1
    assert json.loads(capsys.readouterr().out) == reply


@pytest.mark.parametrize("cmd", [["cancel", "gone"], ["bump", "gone"], ["wait", "gone"]])
def test_a_reply_from_an_older_broker_without_a_code_still_fails(monkeypatch, cmd,
                                                                 capsys):
    """A broker from before error codes says it in text alone."""
    reply = {"ok": False, "error": "no such job"}
    monkeypatch.setattr(client.Client, "_rpc", lambda self, req, read_timeout=None: reply)
    with pytest.raises(client.BrokerError) as ei:
        client.Client(autospawn=False).cancel("gone")
    assert ei.value.code == "no_such_job" and ei.value.reply is reply
    assert client.main(cmd) == 1
    assert json.loads(capsys.readouterr().out) == reply


def test_a_shutting_down_broker_is_waited_out_and_the_submit_retried(monkeypatch):
    replies = iter([{"ok": False, "error": "broker is shutting down",
                     "code": "shutting_down"}, {"ok": True, "job_id": "j2"}])
    gone = []
    monkeypatch.setattr(client.Client, "_rpc",
                        lambda self, req, read_timeout=None: next(replies))
    monkeypatch.setattr(client.Client, "wait_gone",
                        lambda self, timeout_s=30.0: gone.append(1) or True)
    job = jobs.make_job(app="a", device="venu3", chunks=jobs.op_chunks([]))
    assert client.Client(autospawn=False).submit(job) == "j2"
    assert gone == [1]


@pytest.mark.parametrize("reply,code", [
    ({"ok": False, "error": "invalid job: no chunks", "code": "invalid"}, "invalid"),
    ({"ok": False, "error": "invalid job: no chunks"}, "invalid"),       # older broker
    ({"ok": False, "error": "something new"}, "internal"),
])
def test_a_refused_submit_raises_a_typed_broker_error(monkeypatch, reply, code):
    monkeypatch.setattr(client.Client, "_rpc", lambda self, req, read_timeout=None: reply)
    job = jobs.make_job(app="a", device="venu3", chunks=jobs.op_chunks([]))
    with pytest.raises(client.BrokerError) as ei:
        client.Client(autospawn=False).submit(job)
    assert ei.value.code == code and str(ei.value) == reply["error"]
    assert isinstance(ei.value, RuntimeError)     # what `shots` catches per screen


def test_a_wait_timeout_is_returned_not_raised(monkeypatch):
    """Its reply carries the still-running job; the caller needs it."""
    reply = {"ok": False, "error": "wait timeout", "code": "wait_timeout",
             "job": {"id": "j1", "state": "running", "results": []}}
    monkeypatch.setattr(client.Client, "_rpc", lambda self, req, read_timeout=None: reply)
    assert client.Client(autospawn=False).wait("j1", 1) is reply


def test_wait_that_times_out_again_still_says_the_job_is_running(monkeypatch,
                                                                 capsys):
    """`wait` is the re-attachment command; if its own timeout were a dead end
    the user would be back where they started."""
    monkeypatch.setattr(client.Client, "wait",
                        lambda self, job_id, timeout_s=600.0: {
                            "ok": False, "error": "wait timeout",
                            "job": {"id": job_id, "state": "running",
                                    "results": []}})
    assert client.main(["wait", "j9"]) == 1
    assert "ciqkit sim wait j9" in capsys.readouterr().err


# --- nav must honour its own verification ------------------------------------

def _nav_result(reach):
    return {"ok": True, "job": {"id": "n1", "results": [
        {"kind": "reach", "result": reach}]}}


def test_nav_reports_reaching_the_screen(project, monkeypatch):
    monkeypatch.setattr(client.Client, "submit_wait",
                        lambda self, job, timeout_s=0: _nav_result(
                            {"ok": True, "screen": "settings", "anchor": True,
                             "steps": []}))
    assert client.main(["nav", "--to", "settings", "--project", str(project),
                        "--prg", "a.prg"]) == 0


def test_nav_exits_3_when_the_target_screen_did_not_show_its_text(project,
                                                                  monkeypatch,
                                                                  capsys):
    """`nav` exists to answer "did I reach that screen". Exiting 0 while the
    reach step said otherwise makes the command worthless, and quiet about it."""
    monkeypatch.setattr(client.Client, "submit_wait",
                        lambda self, job, timeout_s=0: _nav_result(
                            {"ok": False, "screen": "settings", "anchor": False,
                             "steps": []}))
    assert client.main(["nav", "--to", "settings", "--project", str(project),
                        "--prg", "a.prg", "--anchor", "SETTINGS"]) == 3
    assert "settings" in capsys.readouterr().err


def test_nav_exits_1_when_a_gesture_did_not_land(project, monkeypatch, capsys):
    monkeypatch.setattr(client.Client, "submit_wait",
                        lambda self, job, timeout_s=0: _nav_result(
                            {"ok": False, "screen": "settings", "anchor": None,
                             "steps": [{"ix": 0, "ok": False}],
                             "error": "step 0 (tap) did not change the screen"}))
    assert client.main(["nav", "--to", "settings", "--project", str(project),
                        "--prg", "a.prg"]) == 1
    assert "step 0" in capsys.readouterr().err


def test_nav_passes_when_ocr_was_merely_unavailable(project, monkeypatch):
    """anchor None means the check was skipped, not failed -- a machine with no
    OCR backend must still be able to navigate."""
    monkeypatch.setattr(client.Client, "submit_wait",
                        lambda self, job, timeout_s=0: _nav_result(
                            {"ok": True, "screen": "settings", "anchor": None,
                             "steps": []}))
    assert client.main(["nav", "--to", "settings", "--project", str(project),
                        "--prg", "a.prg", "--anchor", "SETTINGS"]) == 0


def test_the_nav_chunk_budget_grows_with_the_recipe(project, monkeypatch):
    """Every gesture runs in ONE chunk (nav is stateful), so a flat default
    meant a long recipe timed out mid-nav and took a sim restart with it."""
    steps = json.dumps([{"kind": "tap"}] * 10)
    job = _submitted(monkeypatch, ["nav", "--to", "deep", "--project",
                                   str(project), "--prg", "a.prg",
                                   "--steps", steps])
    short = _submitted(monkeypatch, ["nav", "--to", "near", "--project",
                                     str(project), "--prg", "a.prg",
                                     "--steps", json.dumps([{"kind": "tap"}])])
    assert _chunk(job, "reach").max_s > _chunk(short, "reach").max_s
    assert _chunk(job, "reach").max_s > 120


# ── broker interpreter selection ─────────────────────────────────────────────
# The process calling the client often cannot run the broker itself: an MCP
# shim or a build script can import ciqkit over PYTHONPATH and still be a base
# interpreter without Pillow/numpy/pyautogui. Spawning with sys.executable then
# fails, and the error reads like missing deps rather than the wrong python.

def test_broker_candidates_lead_with_env_override(monkeypatch):
    from ciqkit.sim import client

    monkeypatch.setenv("CIQKIT_PYTHON", "/opt/some/python")
    cands = client.broker_python_candidates()
    assert cands[0] == "/opt/some/python"
    assert sys.executable in cands


def test_broker_candidates_are_unique(monkeypatch):
    from ciqkit.sim import client

    monkeypatch.delenv("CIQKIT_PYTHON", raising=False)
    cands = client.broker_python_candidates()
    assert len(cands) == len(set(cands))


def test_resolve_broker_python_skips_unviable(monkeypatch, tmp_path):
    from ciqkit.sim import client

    monkeypatch.setattr(client.proto, "runtime_dir", lambda: str(tmp_path))
    monkeypatch.setattr(client, "broker_python_candidates",
                        lambda: ["/no/such/python", "/usr/bin/works"])
    monkeypatch.setattr(client, "_can_run_broker",
                        lambda p: p == "/usr/bin/works")

    assert client.resolve_broker_python() == "/usr/bin/works"
    # and it is cached for the next spawn
    assert (tmp_path / "broker-python").read_text().strip() == "/usr/bin/works"


def test_resolve_broker_python_reprobes_a_stale_cache(monkeypatch, tmp_path):
    from ciqkit.sim import client

    monkeypatch.setattr(client.proto, "runtime_dir", lambda: str(tmp_path))
    (tmp_path / "broker-python").write_text("/gone/python\n")
    monkeypatch.setattr(client, "broker_python_candidates", lambda: ["/usr/bin/works"])
    monkeypatch.setattr(client, "_can_run_broker", lambda p: p == "/usr/bin/works")

    assert client.resolve_broker_python() == "/usr/bin/works"


def test_spawn_daemon_uses_the_resolved_interpreter(monkeypatch, tmp_path):
    from ciqkit.sim import client

    monkeypatch.setattr(client.proto, "runtime_dir", lambda: str(tmp_path))
    monkeypatch.setattr(client, "resolve_broker_python", lambda: "/usr/bin/works")
    seen = {}

    def fake_popen(argv, **kw):
        seen["argv"] = argv
        return None

    monkeypatch.setattr(client.subprocess, "Popen", fake_popen)
    client.spawn_daemon(str(tmp_path / "sock"))
    assert seen["argv"][0] == "/usr/bin/works"
    assert seen["argv"][1:] == ["-m", "ciqkit.sim.broker"]


def test_spawned_daemon_does_not_inherit_a_held_lease(monkeypatch, tmp_path):
    """A caller under `ciqkit lease sim -- ...` must not hand the daemon its
    "already held" marker: the daemon outlives the caller, and with the marker
    every one of its own `sim` lease acquisitions would be a silent no-op."""
    from ciqkit import leases
    from ciqkit.sim import client

    monkeypatch.setenv(leases.ENV_KEY, "sim")
    monkeypatch.setattr(client.proto, "runtime_dir", lambda: str(tmp_path))
    monkeypatch.setattr(client, "resolve_broker_python", lambda: "/usr/bin/works")
    seen = {}

    def fake_popen(argv, **kw):
        seen["env"] = kw["env"]
        return None

    monkeypatch.setattr(client.subprocess, "Popen", fake_popen)
    client.spawn_daemon(str(tmp_path / "sock"))
    assert leases.ENV_KEY not in seen["env"]
# --- `test` exits on the tests' verdict, not on the job finishing ------------

def _test_result(step):
    return {"ok": True, "job": {"id": "x", "results": [{"kind": "test", "result": step}]}}


@pytest.mark.parametrize("step", [
    {"ok": False, "reason": "no-verdict", "passed": 0, "failed": 0},
    {"ok": False, "reason": "simulator-unreachable", "passed": 0, "failed": 0},
    {"ok": False, "passed": 3, "failed": 1, "errors": 0, "ran": 4},
    {"ok": False, "reason": "zero-tests", "passed": 0, "failed": 0, "ran": 0},
])
def test_a_test_run_that_did_not_pass_exits_nonzero(project, monkeypatch, capsys, step):
    """A DONE job is not a green suite. A test build that crashed on load
    (measured: Windows, no output at all) used to exit 0 and read as passing."""
    monkeypatch.setattr(client.Client, "submit_wait",
                        lambda self, job, timeout_s=0: _test_result(step))
    assert client.main(["test", "--project", str(project), "--prg", "t.prg"]) == 1
    assert "did not pass" in capsys.readouterr().err


def test_a_passing_test_run_exits_zero(project, monkeypatch):
    monkeypatch.setattr(client.Client, "submit_wait",
                        lambda self, job, timeout_s=0: _test_result(
                            {"ok": True, "passed": 1, "failed": 0, "errors": 0, "ran": 1}))
    assert client.main(["test", "--project", str(project), "--prg", "t.prg"]) == 0


def test_project_flag_expands_a_leading_tilde(tmp_path, monkeypatch):
    import argparse
    import os
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    app = tmp_path / "app"
    app.mkdir()
    (app / "manifest.xml").write_bytes(b"<iq:manifest/>")
    ns = argparse.Namespace(project=os.path.join("~", "app"))
    assert client._project_dir(ns) == os.path.abspath(str(app))
    with pytest.raises(SystemExit, match="--project directory does not exist"):
        client._project_dir(argparse.Namespace(project=os.path.join("~", "nope")))


def test_a_cheap_op_gives_up_on_a_broker_that_never_answers(monkeypatch):
    """status/ping/bump/cancel must not inherit WAIT's ten-minute read timeout."""
    import socket as _socket
    from ciqkit.sim import client as client_mod

    srv = _socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)  # accepts at the kernel level, never replies
    try:
        monkeypatch.setattr(client_mod, "CHEAP_RPC_TIMEOUT_S", 0.2)
        c = client_mod.Client("unused", autospawn=False)
        monkeypatch.setattr(
            c, "_connect", lambda: _socket.create_connection(srv.getsockname(), timeout=5))
        import time as _time
        t0 = _time.monotonic()
        with pytest.raises(OSError):  # socket.timeout is an OSError
            c.status()
        assert _time.monotonic() - t0 < 5
    finally:
        srv.close()
