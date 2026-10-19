"""Keep the test suite out of the real runtime directory.

ciqkit's private runtime directory holds the broker socket, lease files and the
seed journal, all keyed on the uid of whoever is running. Without this fixture a
test that seeds writes its journal into the *developer's* directory — and
`ciqkit doctor` then reports a pending seed pointing at a pytest tmpdir that no
longer exists. Tests must never leave anything behind for a human to clean up,
and they must never race a broker the developer has running for real.

Autouse, suite-wide, so a new test cannot forget it.
"""
import os
import shutil
import sys
import tempfile

import pytest


@pytest.fixture(autouse=True)
def private_runtime_dir(tmp_path_factory, monkeypatch):
    # Deliberately NOT under the test's own `tmp_path`: tests point things like
    # sdk_root() at tmp_path and expect it empty, and several modules define
    # their own runtime fixture that would collide on the directory name. This
    # sits beside them and is simply overridden when a module sets its own.
    base = tmp_path_factory.mktemp("ciqkit-runtime")
    base.chmod(0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(base))
    monkeypatch.delenv("CIQKIT_LEASES_HELD", raising=False)
    monkeypatch.delenv("CIQSIM_SOCK", raising=False)
    # The storage journal is persistent by design (~/.local/state) and the sim
    # storage dir is the developer's real simulator: point both at scratch so
    # no test journals, resets or pins anything real.
    monkeypatch.setenv("CIQKIT_STORAGE_JOURNAL", str(base / "storage-journal"))
    monkeypatch.setenv("CIQKIT_SIM_APPS_DIR", str(base / "sim-apps"))


@pytest.fixture
def short_runtime_dir(monkeypatch):
    """A runtime dir whose socket path fits in a sockaddr_un.

    The autouse fixture above points XDG_RUNTIME_DIR at a pytest tmpdir, and
    those nest deep enough that `<dir>/ciqkit/ciqsimd.sock` blows past the ~104
    byte AF_UNIX limit on macOS -- connect() then fails with "path too long"
    rather than "nothing is listening". Any test that actually connects needs
    this; the real runtime dir is short enough in practice.

    On Windows the control channel is loopback TCP and the "socket path" is
    an endpoint file, which has no such limit -- and there is no /tmp.
    """
    base = tempfile.mkdtemp(prefix="ciqk",
                            dir=None if sys.platform == "win32" else "/tmp")
    os.chmod(base, 0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", base)
    yield base
    shutil.rmtree(base, ignore_errors=True)
