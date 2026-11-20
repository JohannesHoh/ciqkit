"""macOS GUI subprocess calls: every one has a timeout, and keys travel as argv."""
import os
import subprocess

import pytest

from ciqkit.capture import screenshot_utils as su


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_run(argv, **kw):
        seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(su.subprocess, "run", fake_run)
    monkeypatch.setattr(su, "IS_MAC", True)
    monkeypatch.setattr(su, "IS_WIN", False)
    monkeypatch.setattr(su.time, "sleep", lambda *_: None)
    # os.getuid does not exist on Windows, where this fixture still fakes macOS
    monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
    return seen


def test_phys_key_quote_is_argv_not_script(calls):
    su.phys_key(None, 'a"b')
    argv, kw = calls[0]
    assert argv[0] == "osascript" and 'a"b' in argv[3:]
    assert 'a"b' not in argv[2]
    assert kw.get("timeout")


def test_phys_key_dash_key_follows_end_of_options(calls):
    # osascript parses a "-x" after the script as its own flag (exit 2, key dropped)
    # unless "--" ends its options first.
    su.phys_key(None, "-x")
    argv, _ = calls[0]
    assert argv.index("--") < argv.index("-x")


def test_phys_key_keycode_has_timeout(calls):
    su.phys_key(None, "up", modifiers=["shift"])
    assert "key code 126" in calls[0][0][2] and calls[0][1].get("timeout")


def test_restore_focus_and_pkill_carry_timeout(calls, monkeypatch):
    monkeypatch.setattr(su, "find_sim_window", lambda *a, **k: None)
    for fn in (su.restore_window, su.focus_window):
        calls.clear()
        try:
            fn(None)
        except Exception:
            pass   # unrelated setup past the osascript call is not under test
        assert calls and all(kw.get("timeout") for _, kw in calls)


def test_timeout_does_not_propagate(monkeypatch):
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(su.subprocess, "run", boom)
    monkeypatch.setattr(su, "IS_MAC", True)
    monkeypatch.setattr(su, "IS_WIN", False)
    monkeypatch.setattr(su.time, "sleep", lambda *_: None)
    su.phys_key(None, "x")
    su._pkill_best_effort(["-x", "simulator"])
    su._osascript_best_effort("return", "t")


def test_orphan_monkeydo_pkill_carries_timeout(calls):
    # a real pkill caller (not the helper itself): a stuck pkill must not stall the
    # restart path, so every call it makes has a timeout
    su._kill_orphan_monkeydo()
    pkills = [(a, kw) for a, kw in calls if a[0] == "pkill"]
    assert pkills and all(kw.get("timeout") for _, kw in pkills)


def test_restart_simulator_pkill_carries_timeout(calls, monkeypatch):
    monkeypatch.setattr(su, "find_sim_window", lambda *a, **k: 1)
    monkeypatch.setattr(su, "ensure_simulator", lambda: 2)
    assert su._restart_simulator() == 2
    pkills = [(a, kw) for a, kw in calls if a[0] == "pkill"]
    assert pkills and all(kw.get("timeout") for _, kw in pkills)


def test_dismiss_alerts_names_travel_as_argv(calls, monkeypatch):
    pn, btn = 'Evil"Proc', 'OK" & (do shell script "x") & "'
    monkeypatch.setattr(su, "_plan_alert_clicks", lambda text: [(pn, 1, btn)])
    assert su._dismiss_system_alerts() == 1
    argv, kw = calls[-1]   # the click call; calls[0] is the scan
    assert argv[0] == "osascript" and argv[argv.index("--") + 1:] == [pn, btn]
    assert argv.index("--") > argv.index("-e") + 1   # "--" comes after the script
    assert pn not in argv[2] and btn not in argv[2]
    assert kw.get("timeout")
