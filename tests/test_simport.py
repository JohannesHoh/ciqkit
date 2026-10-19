"""simport: which port monkeydo will reach, and what is in its way.

Pure decision logic over faked lsof/pgrep output -- no simulator needed.
"""
import sys

import pytest

from ciqkit.sim import simport
from ciqkit.sim.simport import Listener

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shim / macOS crash reports")


def _fake(monkeypatch, listeners, sim_pids, crash=None):
    monkeypatch.setattr(simport, "_lsof_listeners", lambda: listeners)
    monkeypatch.setattr(simport, "_sim_pids", lambda: sim_pids)
    monkeypatch.setattr(simport, "_recent_crash", lambda within_s=900: crash)


def test_foreign_listener_on_1234_is_a_delay_not_a_failure(monkeypatch):
    # The 2026-09-28 layout: LM Studio on 1234, simulator on 1235.
    _fake(monkeypatch, [Listener(1234, 1630, "LM Studio"),
                        Listener(1235, 93847, "simulator")], [93847])
    st = simport.status()
    assert st.reachable and st.port == 1235
    assert [ln.command for ln in st.foreign_before] == ["LM Studio"]
    assert st.probe_penalty_s == simport.SHELL_PROBE_TIMEOUT_S
    assert "LM Studio" in simport.describe(st)


def test_simulator_on_1234_has_no_penalty(monkeypatch):
    _fake(monkeypatch, [Listener(1234, 7, "simulator")], [7])
    st = simport.status()
    assert st.port == 1234 and st.probe_penalty_s == 0


def test_no_simulator_names_the_recent_crash(monkeypatch):
    _fake(monkeypatch, [Listener(1234, 1630, "LM Studio")], [],
          crash="/x/simulator-2026-09-28-083210.ips")
    st = simport.status()
    assert not st.reachable and st.probe_penalty_s == 0
    msg = simport.describe(st)
    assert "not running" in msg and "simulator-2026-09-28-083210.ips" in msg


def test_simulator_beyond_monkeydo_range_is_unreachable(monkeypatch):
    taken = [Listener(p, 100 + p, "other") for p in range(1234, 1239)]
    _fake(monkeypatch, taken + [Listener(1239, 5, "simulator")], [5])
    st = simport.status()
    assert not st.reachable and st.sim_ports == [1239]
    assert "outside" in simport.describe(st)


def test_running_but_unbound_simulator_is_reported_as_starting(monkeypatch):
    _fake(monkeypatch, [], [42])
    st = simport.status()
    assert not st.reachable and "not bound a port yet" in simport.describe(st)


def test_sim_identified_by_pid_even_under_another_command_name(monkeypatch):
    _fake(monkeypatch, [Listener(1236, 42, "Connect IQ Device Simulator")], [42])
    assert simport.status().port == 1236


def test_foreign_loopback_bind_on_the_sims_own_port_shadows_it(monkeypatch):
    """Windows lets a 127.0.0.1 bind sit on a port the simulator holds on
    0.0.0.0, and the specific bind takes monkeydo's connection: a push then
    blocks on a program that is not the simulator. Seen live, 2026-10-08."""
    _fake(monkeypatch, [Listener(1234, 7, "simulator.exe", "0.0.0.0"),
                        Listener(1234, 9, "python.exe", "127.0.0.1")], [7])
    st = simport.status()
    assert not st.reachable and [ln.pid for ln in st.shadowed_by] == [9]
    assert "bound 127.0.0.1:1234 on top of it" in simport.describe(st)
    # A wildcard or other-address bind on the same port takes nothing.
    _fake(monkeypatch, [Listener(1234, 7, "simulator.exe", "0.0.0.0"),
                        Listener(1234, 9, "python.exe", "::")], [7])
    assert simport.status().port == 1234


def _sdk(tmp_path, with_class=True):
    import zipfile
    b = tmp_path / "bin"
    b.mkdir()
    (b / "shell").write_text("")
    (b / "monkeydo").write_text("")
    with zipfile.ZipFile(b / "monkeybrains.jar", "w") as z:
        if with_class:
            z.writestr(simport.MONKEYDO_CLASS.replace(".", "/") + ".class", b"")
    return str(tmp_path)


@posix_only
def test_shim_engages_only_when_a_foreign_listener_is_in_the_way(monkeypatch, tmp_path):
    monkeypatch.setattr(simport.shutil, "which", lambda n: "/usr/bin/java")
    monkeypatch.setattr(simport, "_shim_path", lambda: "/shim.sh")
    sdk = _sdk(tmp_path)
    _fake(monkeypatch, [Listener(1234, 1, "LM Studio"), Listener(1235, 2, "simulator")], [2])
    argv, env, how = simport.monkeydo_command(sdk, "a.prg", "venu3", ["-t"])
    assert how == "shim" and env["CIQ_SIM_PORT"] == "1235"
    assert argv[3:] == [simport.MONKEYDO_CLASS, "-f", "a.prg", "-d", "venu3",
                        "-s", "/shim.sh", "-t"]
    _fake(monkeypatch, [Listener(1234, 2, "simulator")], [2])
    argv, env, how = simport.monkeydo_command(sdk, "a.prg", "venu3", ["-t"])
    assert how == "monkeydo" and env is None and argv[1:] == ["a.prg", "venu3", "-t"]


@posix_only
def test_shim_falls_back_when_the_sdk_lacks_the_class(monkeypatch, tmp_path):
    monkeypatch.setattr(simport.shutil, "which", lambda n: "/usr/bin/java")
    _fake(monkeypatch, [Listener(1234, 1, "LM Studio"), Listener(1235, 2, "simulator")], [2])
    assert simport.monkeydo_command(_sdk(tmp_path, with_class=False),
                                    "a.prg", "venu3")[2] == "monkeydo"


@posix_only
def test_shim_script_passes_only_the_simulator_port(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr("ciqkit.paths.runtime_dir", lambda: str(tmp_path))
    shim = simport._shim_path()
    real = tmp_path / "real.sh"
    real.write_text('#!/bin/sh\necho "real $*"\n')
    real.chmod(0o755)
    env = {"CIQ_SIM_PORT": "1235", "CIQ_REAL_SHELL": str(real), "PATH": "/usr/bin:/bin"}
    hit = subprocess.run([shim, "--transport=tcp", "--transport_args=127.0.0.1:1235"],
                         env=env, capture_output=True, text=True)
    assert hit.returncode == 0 and "real" in hit.stdout
    miss = subprocess.run([shim, "--transport=tcp", "--transport_args=127.0.0.1:1234"],
                          env=env, capture_output=True, text=True)
    assert miss.returncode == 1 and miss.stdout == ""


@posix_only
def test_recent_crash_dates_by_filename_not_mtime(tmp_path, monkeypatch):
    import os
    import time
    old = tmp_path / "simulator-2026-09-25-084245.ips"
    old.write_text("")
    os.utime(old, None)  # touched now, like macOS does
    monkeypatch.setattr(simport, "_CRASH_GLOB", str(tmp_path / "simulator-*.ips"))
    assert simport._recent_crash() is None
    fresh = tmp_path / time.strftime("simulator-%Y-%m-%d-%H%M%S.ips")
    fresh.write_text("")
    assert simport._recent_crash() == str(fresh)


# --- Windows: monkeydo.bat speaks /t, not -t -----------------------------------

def _win_sdk(tmp_path):
    b = tmp_path / "bin"
    b.mkdir()
    for name in ("shell.exe", "monkeybrains.jar", "monkeydo.bat"):
        (b / name).write_text("")
    return str(tmp_path), b


def test_windows_runs_monkeydos_java_line_directly(monkeypatch, tmp_path):
    """bin\\monkeydo.bat only understands /t; the -t every other platform
    passes fell through to its usage text, so a Windows test run never
    reached the simulator. The java line it wraps takes -t as-is."""
    monkeypatch.setattr(simport, "IS_WIN", True)
    monkeypatch.setattr(simport, "java_exe", lambda: r"C:\jdk\bin\java.exe")
    _fake(monkeypatch, [Listener(1234, 2, "simulator.exe")], [2])
    sdk, b = _win_sdk(tmp_path)
    argv, env, how = simport.monkeydo_command(sdk, r"C:\p\a.prg", "venu3", ["-t"])
    assert how == "java" and env is None
    assert argv == [r"C:\jdk\bin\java.exe", "-classpath", str(b / "monkeybrains.jar"),
                    simport.MONKEYDO_CLASS, "-f", r"C:\p\a.prg", "-d", "venu3",
                    "-s", str(b / "shell.exe"), "-t"]


def test_windows_without_java_translates_flags_for_the_batch_file(monkeypatch, tmp_path):
    monkeypatch.setattr(simport, "IS_WIN", True)
    monkeypatch.setattr(simport, "java_exe", lambda: None)
    _fake(monkeypatch, [Listener(1234, 1, "LM Studio"), Listener(1235, 2, "simulator.exe")], [2])
    sdk, b = _win_sdk(tmp_path)
    argv, _env, how = simport.monkeydo_command(sdk, "a.prg", "venu3", ["-t"])
    assert how == "monkeydo"
    assert argv == [str(b / "monkeydo.bat"), "a.prg", "venu3", "/t"]


def test_windows_shim_engages_behind_a_foreign_listener(monkeypatch, tmp_path):
    """MonkeyDoDeux's port probe has no timeout of its own: on Windows a
    listener that never answers held a push forever (measured: >300 s)."""
    import os
    import zipfile
    monkeypatch.setattr(simport, "IS_WIN", True)
    monkeypatch.setattr(simport, "java_exe", lambda: r"C:\jdk\bin\java.exe")
    monkeypatch.setattr(simport, "_shim_path", lambda: r"C:\rt\monkeydo-shell-shim.cmd")
    sdk, b = _win_sdk(tmp_path)
    with zipfile.ZipFile(b / "monkeybrains.jar", "w") as z:
        z.writestr(simport.MONKEYDO_CLASS.replace(".", "/") + ".class", b"")
    _fake(monkeypatch, [Listener(1234, 1, "LM Studio"), Listener(1235, 2, "simulator.exe")], [2])
    argv, env, how = simport.monkeydo_command(sdk, "a.prg", "venu3", ["-t"])
    assert how == "shim" and argv[-3:] == ["-s", r"C:\rt\monkeydo-shell-shim.cmd", "-t"]
    assert env["CIQ_SIM_PORT"] == "1235" and env["CIQ_REAL_SHELL"] == str(b / "shell.exe")
    assert os.path.isfile(env["CIQ_SHIM_RELAY"]) and env["CIQ_SHIM_PYTHON"]


win_only = pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe shim / Win32 relay")


@win_only
def test_windows_shim_script_passes_only_the_simulator_port(tmp_path, monkeypatch):
    import os
    import subprocess
    monkeypatch.setattr("ciqkit.paths.runtime_dir", lambda: str(tmp_path))
    shim = simport._shim_path()
    real = tmp_path / "real.cmd"
    real.write_bytes(b"@echo real %*\r\n")
    env = dict(os.environ, CIQ_SIM_PORT="1235", CIQ_REAL_SHELL=str(real),
               CIQ_SHIM_PYTHON=simport._relay_python(),
               CIQ_SHIM_RELAY=os.path.join(os.path.dirname(simport.__file__), "shellrelay.py"))
    hit = subprocess.run([shim, "--transport=tcp", "--transport_args=127.0.0.1:1235"],
                         env=env, capture_output=True, text=True, timeout=60)
    assert hit.returncode == 0 and "real" in hit.stdout and "127.0.0.1:1235" in hit.stdout
    miss = subprocess.run([shim, "--transport=tcp", "--transport_args=127.0.0.1:1234"],
                          env=env, capture_output=True, text=True, timeout=60)
    assert miss.returncode == 1 and miss.stdout == ""


@win_only
def test_relay_takes_the_shell_down_with_its_parent():
    """MonkeyDoDeux destroys the shim's cmd.exe; TerminateProcess takes no
    children along, and the orphaned shell kept the run from ever exiting."""
    import os
    import subprocess
    import time

    from ciqkit import _winapi
    py = simport._relay_python()
    relay = os.path.join(os.path.dirname(simport.__file__), "shellrelay.py")
    # Stand-ins: `parent` plays cmd.exe, a sleeping python plays shell.exe.
    parent = subprocess.Popen(
        [py, "-c", "import subprocess, sys; subprocess.call(sys.argv[1:])",
         py, relay, "-c", "import time; time.sleep(120)"],
        env=dict(os.environ, CIQ_REAL_SHELL=py))

    def kids(pid):
        return [p for p, pp, _n in _winapi.processes() if pp == pid]

    deadline = time.time() + 30
    while time.time() < deadline and not any(kids(r) for r in kids(parent.pid)):
        time.sleep(0.2)
    (relay_pid,) = kids(parent.pid)
    (shell_pid,) = kids(relay_pid)
    parent.kill()
    parent.wait()
    deadline = time.time() + 10
    while time.time() < deadline and (_winapi.pid_alive(shell_pid)
                                      or _winapi.pid_alive(relay_pid)):
        time.sleep(0.2)
    alive = [p for p in (relay_pid, shell_pid) if _winapi.pid_alive(p)]
    for p in alive:
        _winapi.terminate(p)
    assert not alive
