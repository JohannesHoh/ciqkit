"""Seeding writes into the user's working tree, so it has to be hard to misuse.

Three guarantees, each with a way it used to fail:

* a patch target cannot escape the project directory. `os.path.join(app_dir,
  rel)` looks like containment and is not: an absolute path discards `app_dir`
  entirely and `../` walks out of it. Seeding is reachable from the broker
  socket and from the MCP server, so an unchecked join is a general-purpose
  write primitive.
* a file with uncommitted changes is not patched unless the caller insists,
  because a SIGKILL mid-job leaves the patch in place and `git checkout` only
  rescues work that was committed.
* the originals reach disk before the tree is touched, so a process that dies
  uncatchably is recoverable rather than merely detectable.
"""
import json
import os
import subprocess

import pytest

from ciqkit.sim import seedjournal
from ciqkit.sim.session import SimError, SimSession


@pytest.fixture(autouse=True)
def _private_runtime(tmp_path, monkeypatch):
    base = tmp_path / "xdg"
    base.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(base))


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj"
    (p / "source").mkdir(parents=True)
    (p / "source" / "Constants.mc").write_bytes(b'const SEED_DEMO = false;\n')
    (p / "source" / "App.mc").write_text('class App { var glance = true; }\n')
    return p


@pytest.fixture
def session():
    s = SimSession.__new__(SimSession)
    s._seed_backup = None
    return s


def _git(project, *args):
    subprocess.run(["git", *args], cwd=project, capture_output=True, check=True)


@pytest.fixture
def git_project(project):
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "t@example.invalid")
    _git(project, "config", "user.name", "t")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "initial")
    return project


# ── containment ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("target", [
    "../outside.mc",
    "../../etc/hosts",
    "source/../../outside.mc",
])
def test_a_patch_cannot_escape_the_project_with_a_relative_path(
        session, project, target, tmp_path):
    victim = tmp_path / "outside.mc"
    victim.write_text("untouched\n")
    with pytest.raises(SimError, match="outside the project"):
        session.seed(str(project),
                     patches=[{"file": target, "pattern": "untouched",
                               "repl": "OWNED"}])
    assert victim.read_text() == "untouched\n"


def test_a_patch_cannot_escape_the_project_with_an_absolute_path(
        session, project, tmp_path):
    """os.path.join(app_dir, "/abs") is just "/abs" — the join silently yields."""
    victim = tmp_path / "outside.mc"
    victim.write_text("untouched\n")
    with pytest.raises(SimError, match="outside the project"):
        session.seed(str(project),
                     patches=[{"file": str(victim), "pattern": "untouched",
                               "repl": "OWNED"}])
    assert victim.read_text() == "untouched\n"


def test_a_symlink_out_of_the_project_is_refused(session, project, tmp_path):
    victim = tmp_path / "outside.mc"
    victim.write_text("untouched\n")
    os.symlink(victim, project / "source" / "link.mc")
    with pytest.raises(SimError, match="outside the project"):
        session.seed(str(project),
                     patches=[{"file": "source/link.mc", "pattern": "untouched",
                               "repl": "OWNED"}])
    assert victim.read_text() == "untouched\n"


def test_a_legitimate_in_project_patch_still_works(session, project):
    session.seed(str(project),
                 patches=[{"file": "source/App.mc", "pattern": "glance = true",
                           "repl": "glance = false"}])
    assert "glance = false" in (project / "source" / "App.mc").read_text()
    session.restore()
    assert "glance = true" in (project / "source" / "App.mc").read_text()


# ── the git-dirty guard ───────────────────────────────────────────────────────

def test_uncommitted_changes_are_not_patched_by_default(session, git_project):
    (git_project / "source" / "Constants.mc").write_text(
        'const SEED_DEMO = false;  // work in progress\n')
    with pytest.raises(SimError, match="uncommitted changes"):
        session.seed(str(git_project), {"SEED_DEMO": "true"})
    assert "work in progress" in (git_project / "source" / "Constants.mc").read_text()


def test_force_overrides_the_dirty_guard(session, git_project):
    (git_project / "source" / "Constants.mc").write_text(
        'const SEED_DEMO = false;  // work in progress\n')
    session.seed(str(git_project), {"SEED_DEMO": "true"}, force=True)
    assert "const SEED_DEMO = true;" in (git_project / "source" / "Constants.mc").read_text()
    session.restore()
    assert "work in progress" in (git_project / "source" / "Constants.mc").read_text()


def test_a_committed_file_seeds_without_force(session, git_project):
    session.seed(str(git_project), {"SEED_DEMO": "true"})
    assert "const SEED_DEMO = true;" in (git_project / "source" / "Constants.mc").read_text()


def test_a_project_outside_git_still_seeds(session, project):
    """No repo means no `git checkout` safety net, but the journal covers it."""
    session.seed(str(project), {"SEED_DEMO": "true"})
    assert "const SEED_DEMO = true;" in (project / "source" / "Constants.mc").read_text()


# ── the crash-safe journal ────────────────────────────────────────────────────

def test_the_journal_is_written_before_the_tree_is_touched(session, project):
    session.seed(str(project), {"SEED_DEMO": "true"})
    j = seedjournal.pending()
    assert j and j["pid"] == os.getpid()
    [entry] = j["files"]
    assert entry["path"] == str(project / "source" / "Constants.mc")
    with open(entry["backup"], "rb") as fh:
        assert fh.read() == b'const SEED_DEMO = false;\n'


def test_restore_clears_the_journal(session, project):
    session.seed(str(project), {"SEED_DEMO": "true"})
    session.restore()
    assert seedjournal.pending() is None


def test_a_rejected_seed_leaves_no_journal(session, project):
    with pytest.raises(SimError):
        session.seed(str(project), {"NOT_THERE": "1"})
    assert seedjournal.pending() is None


def _orphan_the_journal():
    """Rewrite the journal as if the process that owned it had been killed."""
    journal = seedjournal.pending()
    journal["pid"] = None
    with open(seedjournal.journal_path(), "w") as fh:
        json.dump(journal, fh)


def test_recover_puts_back_what_a_killed_process_left_patched(session, project):
    """The SIGKILL case: the in-memory backup died with the process."""
    session.seed(str(project), {"SEED_DEMO": "true"})
    session._seed_backup = None                     # as if the process was killed
    _orphan_the_journal()

    restored = seedjournal.recover()

    assert restored == [str(project / "source" / "Constants.mc")]
    assert (project / "source" / "Constants.mc").read_text() == 'const SEED_DEMO = false;\n'
    assert seedjournal.pending() is None


def test_recover_does_not_overwrite_a_file_edited_since_the_crash(session, project):
    """The failure mode that makes automatic recovery worth being careful about.

    Broker is killed mid-seed; the user notices the patch and edits the file
    themselves. Blindly writing the backup back would delete their work to undo
    ours — a worse outcome than the leftover patch.
    """
    constants = project / "source" / "Constants.mc"
    session.seed(str(project), {"SEED_DEMO": "true"})
    session._seed_backup = None
    _orphan_the_journal()
    constants.write_bytes(b'const SEED_DEMO = false;  // fixed by hand\n')

    assert seedjournal.recover() == []
    assert constants.read_text() == 'const SEED_DEMO = false;  // fixed by hand\n'

    # The original is kept, with the evidence a human needs to decide.
    [entry] = seedjournal.stale()
    assert entry["path"] == str(constants)
    with open(entry["backup"], "rb") as fh:
        assert fh.read() == b'const SEED_DEMO = false;\n'


def test_recover_is_quiet_when_the_file_is_already_back_to_normal(session, project):
    constants = project / "source" / "Constants.mc"
    original = constants.read_bytes()
    session.seed(str(project), {"SEED_DEMO": "true"})
    session._seed_backup = None
    _orphan_the_journal()
    constants.write_bytes(original)                 # e.g. `git checkout` got there

    assert seedjournal.recover() == []
    assert seedjournal.stale() == []
    assert seedjournal.pending() is None


def test_a_stale_entry_survives_a_later_seed_of_another_file(session, project):
    """A file awaiting human judgement must not be forgotten by the next job."""
    constants = project / "source" / "Constants.mc"
    session.seed(str(project), {"SEED_DEMO": "true"})
    session._seed_backup = None
    _orphan_the_journal()
    constants.write_bytes(b'const SEED_DEMO = false;  // fixed by hand\n')
    seedjournal.recover()

    session.seed(str(project),
                 patches=[{"file": "source/App.mc", "pattern": "glance = true",
                           "repl": "glance = false"}])
    session.restore()

    assert [e["path"] for e in seedjournal.stale()] == [str(constants)]


def test_recover_leaves_a_live_owners_seed_alone(session, project):
    """A running broker restores from memory; yanking its files mid-job would
    make it build the unseeded source it is halfway through capturing."""
    session.seed(str(project), {"SEED_DEMO": "true"})
    assert seedjournal.recover() == []
    assert "const SEED_DEMO = true;" in (project / "source" / "Constants.mc").read_text()


def test_recover_is_a_noop_with_no_journal():
    assert seedjournal.recover() == []


def test_an_entry_whose_backup_is_gone_is_dropped_not_kept_forever(
        session, project):
    """Nothing can resolve it, so keeping it would nag through `doctor` forever."""
    session.seed(str(project), {"SEED_DEMO": "true"})
    session._seed_backup = None
    _orphan_the_journal()
    [entry] = seedjournal.pending()["files"]
    os.unlink(entry["backup"])

    assert seedjournal.recover() == []
    assert seedjournal.stale() == []
    assert seedjournal.pending() is None


def test_an_unreadable_target_keeps_its_backup(session, project, monkeypatch):
    """A moved project or an unmounted volume is temporary — do not throw the
    original away over it."""
    session.seed(str(project), {"SEED_DEMO": "true"})
    session._seed_backup = None
    _orphan_the_journal()
    constants = project / "source" / "Constants.mc"
    os.unlink(constants)

    assert seedjournal.recover() == []
    [entry] = seedjournal.stale()
    assert entry["path"] == str(constants)
    assert os.path.isfile(entry["backup"])
