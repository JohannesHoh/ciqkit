"""SimSession logic that does not need a simulator.

The build shell-out and the test-result parser are the two places where a
silent wrong answer is expensive: a build that "succeeded" but produced no
.prg, or a test run reported as green because the harness printed nothing.
"""
import os
import subprocess
import sys

import pytest

from ciqkit.sim.session import (DEFAULT_BUILD_CMD, SimError, SimSession,
                                app_identity)


@pytest.fixture
def session():
    s = SimSession.__new__(SimSession)
    s._seed_backup = None
    s._warned_devices = set()
    s.current = None
    s.sdk = "/nonexistent-sdk"
    return s


@pytest.fixture
def project(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "bin").mkdir()
    return tmp_path


def _script(project, body):
    p = project / "scripts" / "build.sh"
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(0o755)
    return p


# --- build -------------------------------------------------------------------

def test_the_prg_path_is_taken_from_the_build_output(session, project):
    out = project / "bin" / "MyApp.prg"
    out.write_text("x")
    _script(project, f'echo "Build successful: {out}"\n')
    assert session.build(str(project)) == str(out)


@pytest.mark.parametrize("line", [
    "Build successful: {p}",
    "BUILD SUCCESSFUL -> {p}",
    "Wrote {p}",
    "  output = '{p}'",
])
def test_the_announced_path_is_found_whatever_the_script_calls_it(
        session, project, line):
    """Build scripts announce their output in whatever shape they like."""
    out = project / "build" / "MyApp.prg"
    out.parent.mkdir(exist_ok=True)
    out.write_text("x")
    _script(project, f'echo "{line.format(p=out)}"\n')
    assert session.build(str(project)) == str(out)


def test_a_path_in_the_output_that_does_not_exist_is_ignored(session, project):
    """A .prg named in an error message is not this build's artefact."""
    real = project / "bin" / "Real.prg"
    _script(project, 'echo "could not open /nope/Ghost.prg"\n'
                     f'touch "{real}"\n')
    assert session.build(str(project)) == str(real)


def test_output_written_to_build_rather_than_bin_is_found(session, project):
    """Not every project builds into bin/ -- ours did not, and the old
    bin-only scan silently returned a stale artefact from somewhere else."""
    _script(project, 'mkdir -p build && touch build/MyApp.prg\n')
    assert session.build(str(project)).endswith(os.path.join("build", "MyApp.prg"))


def test_a_stale_prg_is_never_passed_off_as_a_fresh_build(session, project):
    """The bug this guard exists for.

    A build whose output we cannot parse used to fall back to the newest .prg
    lying around. In a real project that was a three-day-old `-t` unit-test
    binary: it pushed, the screen changed, verification passed, and the
    screenshot was of a build nobody asked for.
    """
    import os
    import time
    stale = project / "bin" / "OldApp-tests.prg"
    stale.write_text("x")
    old = time.time() - 3 * 86400
    os.utime(stale, (old, old))
    _script(project, "true\n")          # succeeds, builds nothing, says nothing
    with pytest.raises(SimError) as exc:
        session.build(str(project))
    assert "stale" in str(exc.value)
    assert stale.name not in str(exc.value.__cause__ or "")


def test_without_that_line_the_newest_FRESH_prg_is_used(session, project):
    """Only artefacts this build actually wrote are candidates."""
    import os
    import time
    old = project / "bin" / "old.prg"
    old.write_text("x")
    os.utime(old, (time.time() - 600, time.time() - 600))
    _script(project, "touch bin/new.prg\n")
    assert session.build(str(project)).endswith("new.prg")


def test_a_failing_build_raises_with_its_output(session, project):
    _script(project, 'echo "monkeyc: syntax error on line 12" >&2\nexit 1\n')
    with pytest.raises(SimError, match="syntax error"):
        session.build(str(project))


def test_a_build_that_produces_nothing_is_not_reported_as_success(session, project):
    """Exit 0 with no artefact must fail, not push whatever was there before."""
    _script(project, "true\n")
    with pytest.raises(SimError, match="produced no .prg"):
        session.build(str(project))


def test_a_missing_build_script_names_the_way_out(session, project, monkeypatch):
    # Asked for by name: with no command at all, a project without the script
    # falls back to the built-in monkeyc build instead.
    monkeypatch.setenv("CIQKIT_BUILD_CMD", DEFAULT_BUILD_CMD)
    with pytest.raises(SimError) as exc:
        session.build(str(project))
    msg = str(exc.value)
    assert "--prg" in msg and "CIQKIT_BUILD_CMD" in msg


def test_a_missing_project_directory_is_rejected(session, tmp_path):
    with pytest.raises(SimError, match="project directory not found"):
        session.build(str(tmp_path / "nope"))


def test_the_build_command_is_overridable(session, project):
    out = project / "bin" / "Custom.prg"
    out.write_text("x")
    # A quoted interpreter path with spaces/backslashes: the shape a Windows
    # CIQKIT_BUILD_CMD takes, which shlex used to mangle.
    cmd = f'"{sys.executable}" -c "pass"'
    assert session.build(str(project), build_cmd=cmd) == str(out)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows build argv")
def test_windows_keeps_backslashes_and_runs_sh_through_git_bash(project, monkeypatch):
    from ciqkit import winbash
    from ciqkit.sim.session import _build_argv
    monkeypatch.setattr(winbash, "git_bash", lambda: r"C:\Git\bin\bash.exe")
    argv = _build_argv(r'C:\sdk\bin\monkeyc.bat -d venu3 -o "C:\my dir\a.prg"', str(project))
    assert argv == [r"C:\sdk\bin\monkeyc.bat", "-d", "venu3", "-o", r"C:\my dir\a.prg"]
    # Never the WSL bash.exe on PATH: Git's, found next to git.
    assert _build_argv("./scripts/build.sh -d venu3", str(project)) == [
        r"C:\Git\bin\bash.exe", "./scripts/build.sh", "-d", "venu3"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows build argv")
def test_windows_without_git_bash_falls_back_to_a_native_script(project, monkeypatch):
    from ciqkit import winbash
    from ciqkit.sim.session import _build_argv
    monkeypatch.setattr(winbash, "git_bash", lambda: None)
    with pytest.raises(SimError, match="Git for Windows"):
        _build_argv("./scripts/build.sh -d venu3", str(project))
    (project / "scripts" / "build.cmd").write_text("@echo off\r\n")
    argv = _build_argv("./scripts/build.sh -d venu3", str(project))
    assert argv == [str(project / "scripts" / "build.cmd"), "-d", "venu3"]


def test_the_device_is_substituted_into_the_build_command(session, project):
    _script(project, 'touch "bin/app-$2.prg"\n')
    got = session.build(str(project), device="fr265",
                        build_cmd="./scripts/build.sh -d {device}")
    assert got.endswith("app-fr265.prg")


def test_the_default_command_is_the_documented_one():
    assert DEFAULT_BUILD_CMD == "./scripts/build.sh -d {device}"


def test_a_hanging_build_times_out_instead_of_blocking_forever(session, project):
    _script(project, "sleep 30\n")
    with pytest.raises(SimError, match="timed out"):
        session.build(str(project), timeout_s=0.5)


def test_a_build_command_that_does_not_exist_is_reported_clearly(session, project):
    with pytest.raises(SimError, match="not executable"):
        session.build(str(project), build_cmd="definitely-not-a-command")


# --- unit-test result parsing ------------------------------------------------

def _reachable(monkeypatch):
    from ciqkit.sim import simport
    st = simport.Status(sim_pids=[1], sim_ports=[1234], port=1234)
    monkeypatch.setattr(simport, "wait_for_port", lambda *a, **k: st)
    monkeypatch.setattr(simport, "monkeydo_command",
                        lambda sdk, prg, dev, extra=(), st=None: (["monkeydo"], None, "monkeydo"))


def _fake_run(monkeypatch, stdout, returncode=0):
    _reachable(monkeypatch)

    def run(*_a, **_k):
        return subprocess.CompletedProcess([], returncode, stdout, "")
    monkeypatch.setattr(subprocess, "run", run)


def test_a_passing_run_is_parsed(session, monkeypatch):
    monkeypatch.setattr(SimSession, "ensure", lambda self, *a, **k: {"ok": True})
    _fake_run(monkeypatch, "RESULTS\nPassed: 12  Failed: 0\n")
    r = session.run_tests("x.prg")
    assert (r["ok"], r["passed"], r["failed"]) == (True, 12, 0)


def test_a_failing_run_is_not_reported_as_green(session, monkeypatch):
    monkeypatch.setattr(SimSession, "ensure", lambda self, *a, **k: {"ok": True})
    _fake_run(monkeypatch, "RESULTS\nPassed: 9  Failed: 3\n")
    r = session.run_tests("x.prg")
    assert r["ok"] is False and r["failed"] == 3


def test_a_run_with_no_verdict_is_never_green(session, monkeypatch):
    """A build made without `monkeyc -t` contains no cases, and SDK 9.1.0
    prints nothing at all for it. No verdict is not a pass."""
    monkeypatch.setattr(SimSession, "ensure", lambda self, *a, **k: {"ok": True})
    _fake_run(monkeypatch, "no tests here\n")
    r = session.run_tests("x.prg")
    assert r["ok"] is False and r["passed"] == 0 and r["reason"] == "no-verdict"


def test_unable_to_connect_is_not_a_pass_with_zero_failures(session, monkeypatch):
    monkeypatch.setattr(SimSession, "ensure", lambda self, *a, **k: {"ok": True})
    _fake_run(monkeypatch, "Unable to connect to simulator.\n", returncode=1)
    r = session.run_tests("x.prg")
    assert r["ok"] is False and r["reason"] == "simulator-unreachable"


def test_the_sdk_9_verdict_line_is_parsed(session, monkeypatch):
    monkeypatch.setattr(SimSession, "ensure", lambda self, *a, **k: {"ok": True})
    _fake_run(monkeypatch, "fooTest  PASS\nRan 225 tests\n\n"
                           "PASSED (passed=225, failed=0, errors=0)\n", returncode=1)
    r = session.run_tests("x.prg")
    assert (r["ok"], r["passed"], r["ran"]) == (True, 225, 225)
    _fake_run(monkeypatch, "Ran 3 tests\n\nFAILED (passed=2, failed=0 , errors=1)\n")
    r = session.run_tests("x.prg")
    assert r["ok"] is False and r["errors"] == 1


def test_a_zero_test_verdict_is_not_green(session, monkeypatch):
    monkeypatch.setattr(SimSession, "ensure", lambda self, *a, **k: {"ok": True})
    _fake_run(monkeypatch, "Ran 0 tests\n\nPASSED (passed=0, failed=0, errors=0)\n")
    r = session.run_tests("x.prg")
    assert r["ok"] is False and r["reason"] == "zero-tests"


def test_an_unreachable_simulator_is_reported_before_running(session, monkeypatch):
    from ciqkit.sim import simport
    monkeypatch.setattr(SimSession, "ensure", lambda self, *a, **k: {"ok": True})
    monkeypatch.setattr(simport, "wait_for_port", lambda *a, **k: simport.Status())
    r = session.run_tests("x.prg")
    assert r["ok"] is False and r["reason"] == "simulator-unreachable"


def test_a_test_run_that_hangs_is_reported_as_a_timeout(session, monkeypatch):
    monkeypatch.setattr(SimSession, "ensure", lambda self, *a, **k: {"ok": True})
    _reachable(monkeypatch)

    def run(*_a, **_k):
        raise subprocess.TimeoutExpired("monkeydo", 1, output="partial")
    monkeypatch.setattr(subprocess, "run", run)
    r = session.run_tests("x.prg", timeout_s=1)
    assert r["ok"] is False and r["reason"] == "timeout"


# --- device resolution -------------------------------------------------------

def test_known_devices_resolve_to_their_face_size(session):
    assert session._device_res("venu3") == 454
    assert session._device_res("fr265") == 416


def test_the_sdk_profile_beats_the_hand_written_table(session, tmp_path, monkeypatch):
    """Regression: fr265s is a 360px face, DEVICE_RES had no entry, and the
    454 fallback mis-snapped every crop of that run. The SDK ships the answer
    in compiler.json, so read it instead of guessing."""
    import json
    from ciqkit.sim import session as sess_mod
    (tmp_path / "fr265s").mkdir()
    (tmp_path / "fr265s" / "compiler.json").write_text(
        json.dumps({"resolution": {"width": 360, "height": 360}}))
    monkeypatch.setattr(SimSession, "SDK_DEVICES_DIR", str(tmp_path))
    monkeypatch.setattr(sess_mod, "_SDK_RES_CACHE", {})
    assert session._device_res("fr265s") == 360


def test_an_unknown_device_falls_back_and_warns_once(session, caplog):
    with caplog.at_level("WARNING"):
        assert session._device_res("no-such-watch") == SimSession.DEFAULT_DEVICE_RES
        assert session._device_res("no-such-watch") == SimSession.DEFAULT_DEVICE_RES
    assert sum("no-such-watch" in r.message for r in caplog.records) == 1


# --- load verification -------------------------------------------------------

def test_two_worktrees_with_the_same_folder_name_are_different_apps():
    """The warm-load key must identify the CHECKOUT, not the folder basename.

    Parallel agents work in git worktrees of one repo, so `MyWatchApp` is the
    basename of several directories at once. Keyed on the basename, job B's
    warm-skip declined to push and handed back job A's screen as a verified
    screenshot of B's build.
    """
    a = app_identity("MyWatchApp", project="/tmp/wt-a/MyWatchApp")
    b = app_identity("MyWatchApp", project="/tmp/wt-b/MyWatchApp")
    assert a != b


def test_two_apps_built_from_one_directory_are_different_apps():
    proj = "/tmp/monorepo"
    assert app_identity("watchface", project=proj) != app_identity("widget", project=proj)


def test_the_identity_of_a_project_is_stable_across_the_paths_it_can_arrive_by():
    """project=, app_dir= and a .prg inside it must all name the same app."""
    import os
    proj = os.path.realpath("/tmp")
    by_project = app_identity("App", project=proj)
    by_app_dir = app_identity("App", app_dir=proj)
    by_prg = app_identity("App", prg=os.path.join(proj, "bin", "App.prg"))
    assert by_project == by_app_dir == by_prg


def test_a_frame_without_a_baseline_is_not_accepted_as_a_loaded_app(session):
    """No pre-push baseline => the "the screen changed" proof is missing.

    `_safe_sig` swallows capture errors, so this is not only the cold-start
    case it was once assumed to be: one transient grab failure at push time
    landed here too. If the push then silently failed, the device watchface is
    what is on screen -- not blank, not the loader, nothing to differ from --
    and the old code called that a verified load.
    """
    session._is_blank = lambda img: False
    session._is_loading_screen = lambda img: False
    assert session._is_app_frame(object(), before_sig=None, saw_loader=False) is False
    # Seeing the loader come and go IS real evidence, and still counts.
    assert session._is_app_frame(object(), before_sig=None, saw_loader=True) is True


def test_a_load_reports_which_evidence_carried_it(session, monkeypatch):
    """`verified` must name the proof, so a caller can tell strong from weak."""
    # **kw: poll-loop callers pass grab_settle_s through _poll_capture.
    monkeypatch.setattr(session, "_safe_capture", lambda **_kw: object())
    monkeypatch.setattr(session, "_is_loading_screen", lambda img: False)
    monkeypatch.setattr(session, "_is_app_frame", lambda *a, **k: True)
    monkeypatch.setattr("ciqkit.sim.session.su.dismiss_modal_dialogs",
                        lambda **k: None)
    monkeypatch.setattr("ciqkit.sim.session.ocr.contains_any",
                        lambda img, anchor: True)
    ok, confirmed, how = session._verify_loaded(before_sig=(1, 2), timeout=5,
                                                anchor=["HOME"])
    assert (ok, confirmed, how) == (True, True, "anchor")


def test_the_baseline_capture_is_retried_before_being_given_up_on(session, monkeypatch):
    calls = []

    def flaky(**_kw):
        calls.append(1)
        return None if len(calls) < 3 else object()

    monkeypatch.setattr(session, "_safe_capture", flaky)
    monkeypatch.setattr(session, "_face_sig", lambda img: ("sig",))
    assert session._safe_sig() == ("sig",)
    assert len(calls) == 3, "a single transient capture failure lost the baseline"


def test_a_cold_start_connect_refusal_is_retried_once(session, monkeypatch):
    """The port can be listening a moment before monkeydo is accepted."""
    monkeypatch.setattr(SimSession, "ensure", lambda self, *a, **k: {"ok": True})
    monkeypatch.setattr(SimSession, "TEST_RETRY_SETTLE_S", 0)
    _reachable(monkeypatch)
    outs = iter(["Unable to connect to simulator.\n",
                 "Ran 2 tests\n\nPASSED (passed=2, failed=0, errors=0)\n"])
    calls = []

    def run(*_a, **_k):
        calls.append(1)
        return subprocess.CompletedProcess([], 1, next(outs), "")
    monkeypatch.setattr(subprocess, "run", run)
    r = session.run_tests("x.prg")
    assert r["ok"] is True and len(calls) == 2


def test_a_persistent_connect_refusal_is_retried_only_once(session, monkeypatch):
    monkeypatch.setattr(SimSession, "ensure", lambda self, *a, **k: {"ok": True})
    monkeypatch.setattr(SimSession, "TEST_RETRY_SETTLE_S", 0)
    _reachable(monkeypatch)
    calls = []

    def run(cmd, *_a, **_k):
        if cmd == ["monkeydo"]:   # _kill_orphan_monkeydo shells out too
            calls.append(1)
        return subprocess.CompletedProcess([], 1, "Unable to connect to simulator.\n", "")
    monkeypatch.setattr(subprocess, "run", run)
    r = session.run_tests("x.prg")
    assert r["ok"] is False and r["reason"] == "simulator-unreachable" and len(calls) == 2


def test_monkeydo_sweep_takes_the_whole_tree_and_orphaned_shells(monkeypatch):
    """Windows TerminateProcess takes no children along: every killed push
    left its shell.exe running and connected to the simulator, one more per
    push. The sweep follows only monkeydo's own child names down the tree."""
    import types

    import ciqkit
    from ciqkit.capture import screenshot_utils as su
    procs = [  # (pid, ppid, name)
        (10, 1, "java.exe"),        # monkeydo push, ours
        (11, 10, "cmd.exe"),        # simport's shim ...
        (12, 11, "python.exe"),     # ... its relay ...
        (13, 12, "shell.exe"),      # ... and the real shell
        (14, 10, "notepad.exe"),    # not a monkeydo child name: left alone
        (20, 999, "shell.exe"),     # orphan from an earlier push (parent gone)
        (21, 999, "shell.exe"),     # orphan, but not an SDK shell command line
        (30, 1, "java.exe"),        # some other java program
        (40, 1, "java.exe"),        # monkeydo, another logon session
        (41, 40, "shell.exe"),
    ]
    cmd = {10: "java ... MonkeyDoDeux -f a.prg", 30: "java -jar ide.jar",
           40: "java ... MonkeyDoDeux", 20: "shell.exe --transport=tcp "
           "--transport_args=127.0.0.1:1234", 21: "shell.exe"}
    killed = []
    fake = types.SimpleNamespace(
        processes=lambda: procs,
        session_id=lambda pid=None: 2 if pid == 40 else 1,
        command_line=cmd.get,
        terminate=killed.append)
    monkeypatch.setattr(ciqkit, "_winapi", fake, raising=False)
    monkeypatch.setitem(sys.modules, "ciqkit._winapi", fake)
    su._kill_monkeydo_trees_win()
    assert sorted(killed) == [10, 11, 12, 13, 20]
