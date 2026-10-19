"""Seeding must write the value the caller asked for, verbatim.

The value used to be interpolated into a regular-expression *replacement
template*, where backslashes are escapes: a seed of `"\\1"` spliced the matched
text back into the user's source file, and `"a\\g"` raised. Seeds legitimately
carry Windows paths and escaped strings, and a corrupted source file then
builds into the screenshot you were trying to take.
"""
import pytest

from ciqkit.sim.session import SimError, SimSession


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    """Keep the crash-recovery journal out of the developer's real runtime dir.

    seed() writes originals to ciqkit's private runtime directory so a SIGKILL
    can be recovered from. Without this, running the tests leaves journal
    entries pointing at pytest temp paths in the machine's actual runtime
    directory -- which then show up in `ciqkit doctor` and get acted on by
    `ciqkit sim restore`.
    """
    base = tmp_path / "xdg"
    base.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(base))


@pytest.fixture
def project(tmp_path):
    (tmp_path / "source").mkdir()
    (tmp_path / "source" / "Constants.mc").write_text(
        'const SEED_PATH = "old";\nconst SEED_N = 1;\n')
    return tmp_path


@pytest.fixture
def session():
    s = SimSession.__new__(SimSession)
    s._seed_backup = None
    return s


def _constants(project):
    return (project / "source" / "Constants.mc").read_text()


@pytest.mark.parametrize("value", [
    'true',
    '42',
    '"plain"',
    r'"C:\temp\out"',      # backslashes: read as escapes by a template
    r'"\1x"',              # a backreference: used to splice in the match
    r'"a\gb"',             # invalid group syntax: used to raise
])
def test_the_value_is_written_verbatim(session, project, value):
    session.seed(str(project), {"SEED_PATH": value})
    assert f"const SEED_PATH = {value};" in _constants(project)


def test_restore_is_byte_exact(session, project):
    before = (project / "source" / "Constants.mc").read_bytes()
    session.seed(str(project), {"SEED_PATH": r'"\1"', "SEED_N": "99"})
    assert (project / "source" / "Constants.mc").read_bytes() != before
    session.restore()
    assert (project / "source" / "Constants.mc").read_bytes() == before


def test_restore_is_idempotent(session, project):
    assert session.restore() == {"ok": True, "noop": True}


def test_an_unknown_constant_is_rejected(session, project):
    with pytest.raises(SimError, match="not found"):
        session.seed(str(project), {"NOT_THERE": "1"})


def test_a_failed_seed_leaves_no_partial_edit(session, project):
    """One bad name in a batch must not leave the good ones applied."""
    before = (project / "source" / "Constants.mc").read_bytes()
    with pytest.raises(SimError):
        session.seed(str(project), {"SEED_N": "99", "NOT_THERE": "1"})
    assert (project / "source" / "Constants.mc").read_bytes() == before
    assert session._seed_backup is None


def test_seeding_twice_without_restoring_is_refused(session, project):
    session.seed(str(project), {"SEED_N": "2"})
    with pytest.raises(SimError, match="already active"):
        session.seed(str(project), {"SEED_N": "3"})
