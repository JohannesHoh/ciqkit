"""The base install has no dependencies, so half the commands cannot import.

`pip install ciqkit` deliberately pulls in nothing — the broker, leases and
doctor are stdlib-only. That is a feature until somebody runs `ciqkit layout`
on it and gets a bare `ModuleNotFoundError: numpy` traceback, which reads like a
broken package rather than "you have not chosen an extra yet". These tests pin
the message instead.
"""
import importlib

import pytest

from ciqkit import cli


@pytest.fixture
def missing(monkeypatch):
    """Make importing a subcommand's module fail as a missing dependency would."""
    def _missing(name):
        def fake(module):
            raise ImportError(f"No module named {name!r}", name=name)
        monkeypatch.setattr(importlib, "import_module", fake)
    return _missing


@pytest.mark.parametrize("cmd, extra", [("layout", "checks"),
                                        ("shot-score", "checks"),
                                        ("icon", "checks"),
                                        ("sim", "sim")])
def test_a_missing_dependency_names_the_extra_to_install(cmd, extra, missing):
    missing("numpy")
    with pytest.raises(SystemExit) as exc:
        cli.main([cmd, "--help"])
    msg = str(exc.value)
    assert "numpy" in msg
    # Double quotes: single-quoted, cmd.exe and PowerShell hand pip the quotes
    # themselves and the command the user copies fails.
    assert f'pip install "ciqkit[{extra}]"' in msg
    assert "ciqkit doctor" in msg


def test_leases_and_the_broker_import_on_every_platform():
    """`lease` and `sim` used to be POSIX-only (fcntl, AF_UNIX, uids). They
    must now import wherever the suite runs, Windows included."""
    for mod in ("ciqkit.lease_cli", "ciqkit.leases", "ciqkit.sim.client",
                "ciqkit.sim.broker", "ciqkit.sim.storage"):
        importlib.import_module(mod)


def test_every_command_in_the_usage_text_can_be_dispatched():
    """The usage text and the dispatch table drifting apart is a silent 'unknown
    command' for something we told the user to run."""
    listed = {line.split()[0] for line in cli._USAGE.splitlines()
              if line.startswith("  ") and line.strip()
              and not line.strip().startswith("Run ")}
    assert listed == set(cli._COMMANDS)


def test_an_unknown_command_prints_the_usage(capsys):
    assert cli.main(["nope"]) == 2
    err = capsys.readouterr().err
    assert "unknown command" in err
    assert "shot-score" in err


def test_help_and_version_need_no_dependencies(capsys):
    assert cli.main([]) == 0
    assert "commands:" in capsys.readouterr().out
    assert cli.main(["--version"]) == 0
    assert "ciqkit" in capsys.readouterr().out
