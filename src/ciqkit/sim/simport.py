"""Where the Connect IQ Simulator is listening, and what else is in the way.

How monkeydo finds the simulator (read out of SDK 9.1.0's monkeybrains.jar,
``ShellUtils.findSimulatorPort``; there is no port flag): it walks
127.0.0.1:1234..1238 in order, spawns ``bin/shell --transport=tcp
--transport_args=127.0.0.1:<port>`` for each, and takes the first port whose
shell prints ``Shell Version``. The simulator binds the first free port from
1234 up. When none of the five answers, monkeydo prints
``Unable to connect to simulator.``

So another program on 1234 does NOT stop monkeydo reaching a simulator on 1235
-- measured 2026-09-28 with LM Studio on 127.0.0.1:1234 and the simulator on
*:1235, a 221-test BA3 run passed. What it costs is time: the foreign server
accepts the TCP connection but never speaks the device protocol, so the shell
sits until its own ``Timeout waiting for device info`` (~16 s) before monkeydo
moves on. Every push and every test run pays that, which is enough to push a
load past a verify deadline tuned for a simulator on 1234.

``Unable to connect to simulator.`` therefore means: no simulator process, one
that has not bound its port yet (a just-launched or just-restarted one), or
one bound beyond 1238 because five ports were taken. :func:`status` tells those
apart; :func:`wait_for_port` covers the just-launched case.

Neither end is configurable, so ciqkit cannot simply use another range: the
simulator has no port setting (no flag, env var or simulator.ini key) and the
1234..1238 walk is a pair of bytecode constants. What IS configurable is the
shell monkeydo spawns per probe (MonkeyDoDeux ``-s``). :func:`monkeydo_command`
points it at a two-line shim that hands the simulator's own port to the real
shell and refuses every other port instantly, so a foreign listener costs
nothing. It only engages when a foreign listener is actually in the way.

The probe (ShellUtils.findSimulatorPort) has no timeout of its own: it reads
the shell's output until "Shell Version" or EOF. The ~16 s above is how long
bin/shell takes to give up on LM Studio; against a listener that accepts and
never answers it waits forever (on Windows: a push still hanging after 300 s).
On Windows the shim is a batch file plus shellrelay.py -- see ``_SHIM_CMD``.

CLI (used by the monorepo's run_ciq_tests.sh):

    python -m ciqkit.sim.simport [--start] [--wait SECONDS] [--sdk DIR]

exit 0 = monkeydo can reach a simulator, 3 = it cannot (reason printed).

    python -m ciqkit.sim.simport exec-monkeydo --sdk DIR -- <prg> <device> [-t ...]

runs monkeydo (through the shim when a foreign listener is in the way).
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field

MONKEYDO_PORTS = range(1234, 1239)   # ShellUtils.findSimulatorPort, SDK 9.1.0
SHELL_PROBE_TIMEOUT_S = 16           # measured: bin/shell vs a non-simulator listener
IS_WIN = sys.platform == "win32"
# ConnectIQ.app/Contents/MacOS/simulator; bin\simulator.exe on Windows.
SIM_PROCESS = "simulator.exe" if IS_WIN else "simulator"
_CRASH_GLOB = "~/Library/Logs/DiagnosticReports/simulator-*.ips"


@dataclass
class Listener:
    port: int
    pid: int
    command: str
    addr: str = ""   # local address: "127.0.0.1", "0.0.0.0", "*", "::" ...


@dataclass
class Status:
    sim_pids: list = field(default_factory=list)
    sim_ports: list = field(default_factory=list)       # every port a simulator listens on
    port: int | None = None                             # the one monkeydo will pick
    foreign_before: list = field(default_factory=list)  # non-sim listeners monkeydo probes first
    recent_crash: str | None = None
    # A foreign 127.0.0.1 bind on the simulator's own (wildcard) port. Windows
    # allows it without any socket option, and the more specific bind gets
    # every connection to 127.0.0.1 -- monkeydo's included.
    shadowed_by: list = field(default_factory=list)

    @property
    def reachable(self) -> bool:
        return self.port is not None

    @property
    def probe_penalty_s(self) -> int:
        """Seconds every monkeydo call spends on foreign listeners first."""
        return SHELL_PROBE_TIMEOUT_S * len(self.foreign_before)


def _win_listeners() -> list[Listener]:
    """Listening sockets from the TCP table itself: `netstat`'s text is
    localised ("ABHÖREN" on a German box), so parsing it finds nothing."""
    from .._winapi import processes, tcp_listeners
    try:
        names = {pid: name for pid, _ppid, name in processes()}
        return [Listener(port, pid, names.get(pid, "?"), addr)
                for port, pid, addr in tcp_listeners()]
    except OSError:
        return []


def _lsof_listeners() -> list[Listener]:
    if IS_WIN:
        return _win_listeners()
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpcn"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found, pid, cmd = [], 0, ""
    for line in out.splitlines():
        tag, val = line[:1], line[1:]
        if tag == "p":
            pid = int(val)
        elif tag == "c":
            cmd = val.replace("\\x20", " ")
        elif tag == "n":
            addr, _, port = val.rpartition(":")
            if port.isdigit():
                found.append(Listener(int(port), pid, cmd, addr.strip("[]")))
    return found


def _sim_pids() -> list[int]:
    if IS_WIN:
        from .._winapi import pids_named
        try:
            return pids_named(SIM_PROCESS)
        except OSError:
            return []
    try:
        out = subprocess.run(["pgrep", "-u", str(os.getuid()), "-x", SIM_PROCESS],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(p) for p in out.split() if p.isdigit()]


def _recent_crash(within_s: float = 900) -> str | None:
    """Newest simulator crash report from the last `within_s` seconds. Dated
    by the timestamp in the file NAME: macOS rewrites old reports' mtime
    (a 09-25 report read as "crashed just now" on 09-28)."""
    if IS_WIN:
        return None   # Windows Error Reporting keeps no per-app report to point at
    newest, newest_t = None, 0.0
    for path in glob.glob(os.path.expanduser(_CRASH_GLOB)):
        stamp = os.path.basename(path)[len("simulator-"):-len(".ips")]
        try:
            t = time.mktime(time.strptime(stamp[:17], "%Y-%m-%d-%H%M%S"))
        except ValueError:
            continue
        if t > newest_t:
            newest, newest_t = path, t
    return newest if newest and time.time() - newest_t < within_s else None


def status() -> Status:
    st = Status(sim_pids=_sim_pids(), recent_crash=_recent_crash())
    listeners = _lsof_listeners()

    def is_sim(ln):
        # Case-sensitive on macOS on purpose: Apple's own iOS `Simulator` is
        # not ours. Windows image names are case-insensitive.
        cmd = ln.command.lower() if IS_WIN else ln.command
        return cmd == SIM_PROCESS or ln.pid in st.sim_pids

    st.sim_ports = sorted({ln.port for ln in listeners if is_sim(ln)})
    for port in MONKEYDO_PORTS:
        here = [ln for ln in listeners if ln.port == port]
        if not here:
            continue
        if any(is_sim(ln) for ln in here):
            shadow = [ln for ln in here if not is_sim(ln) and ln.addr == "127.0.0.1"]
            if not shadow:
                st.port = port
                break
            # monkeydo's probe reaches the shadowing program, not the
            # simulator, and blocks on it like on any other foreign listener.
            st.shadowed_by = shadow
            continue
        st.foreign_before.append(here[0])
    if st.port is None:
        st.foreign_before = []   # nothing reachable; the penalty is moot
    return st


def describe(st: Status) -> str:
    """One actionable paragraph: why monkeydo can or cannot reach the simulator."""
    lines = []
    if st.reachable:
        lines.append(f"simulator listening on 127.0.0.1:{st.port} (pid "
                     f"{', '.join(map(str, st.sim_pids)) or '?'}) - monkeydo will find it.")
        for ln in st.foreign_before:
            lines.append(
                f"note: {ln.command} (pid {ln.pid}) holds port {ln.port}, which monkeydo probes "
                f"first and waits on until that program answers or hangs up: "
                f"~{SHELL_PROBE_TIMEOUT_S}s for LM Studio, forever for one that does neither. "
                f"ciqkit's own pushes and `exec-monkeydo` skip the probe; the SDK's "
                f"monkeydo does not. Moving that program off "
                f"{MONKEYDO_PORTS.start}-{MONKEYDO_PORTS.stop - 1} removes the problem.")
        return "\n".join(lines)
    if st.shadowed_by:
        ln = st.shadowed_by[0]
        lines.append(
            f"the simulator listens on port {ln.port}, but {ln.command} (pid {ln.pid}) "
            f"bound 127.0.0.1:{ln.port} on top of it and gets monkeydo's connection "
            f"instead, so pushes hang. Quit {ln.command} or move it off "
            f"{MONKEYDO_PORTS.start}-{MONKEYDO_PORTS.stop - 1}.")
        return "\n".join(lines)
    if not st.sim_pids:
        lines.append("the Connect IQ simulator is not running, so monkeydo reports "
                     "'Unable to connect to simulator.'")
        if st.recent_crash:
            lines.append(f"it crashed recently: {st.recent_crash}")
    elif st.sim_ports:
        lines.append(f"the simulator listens on {st.sim_ports}, outside the "
                     f"{MONKEYDO_PORTS.start}-{MONKEYDO_PORTS.stop - 1} range monkeydo "
                     f"scans - other programs hold those ports. Quit the simulator, free "
                     f"a port in that range, relaunch.")
    else:
        lines.append(f"a simulator process is running (pid "
                     f"{', '.join(map(str, st.sim_pids))}) but has not bound a port yet: "
                     f"still starting, or wedged.")
    return "\n".join(lines)


def wait_for_port(timeout: float = 20.0, poll: float = 0.5) -> Status:
    """Poll until monkeydo could reach a simulator, or the timeout passes.
    A just-launched simulator shows its window before it binds the port; a
    push in that gap fails with 'Unable to connect' and nothing says why."""
    deadline = time.time() + timeout
    st = status()
    while not st.reachable and time.time() < deadline:
        time.sleep(poll)
        st = status()
    return st


def launch(sdk: str) -> None:
    """Start the simulator the one way macOS accepts (LaunchServices; a direct
    Popen of the binary aborts as a background process)."""
    app = os.path.join(sdk, "bin", "ConnectIQ.app")
    if sys.platform == "darwin" and os.path.isdir(app):
        subprocess.Popen(["open", "-a", app],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    exe = os.path.join(sdk, "bin", SIM_PROCESS)
    if not os.path.exists(exe):
        raise FileNotFoundError(f"no simulator in {os.path.join(sdk, 'bin')}")
    # Detached: the simulator must outlive whoever happened to start it (a
    # broker that idles out, a CLI call inside an IDE's job object).
    from .._compat import detached_popen
    detached_popen([exe], gui=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)


MONKEYDO_CLASS = "com.garmin.monkeybrains.monkeydodeux.MonkeyDoDeux"
_SHIM = """#!/bin/sh
# ciqkit monkeydo shell shim (ciqkit.sim.simport): monkeydo probes
# 127.0.0.1:1234..1238 through this. Hand the simulator's own port to the real
# shell; refuse every other port at once instead of letting the real shell
# wait ~16 s on a foreign listener (LM Studio on 1234).
for a in "$@"; do
  if [ "$a" = "--transport_args=127.0.0.1:$CIQ_SIM_PORT" ]; then
    exec "$CIQ_REAL_SHELL" "$@"
  fi
done
exit 1
"""
# Windows: Java runs a .cmd through cmd.exe, so the shim is a batch file, and
# with no exec it cannot BECOME the real shell. MonkeyDoDeux ends every shell
# with Process.destroy(), which on Windows kills cmd.exe alone; an orphaned
# shell.exe kept MonkeyDoDeux's pipe open and the run never exited. So the
# simulator's port goes to shellrelay.py, which takes shell.exe down with
# cmd.exe. Delayed expansion compares the arguments without cmd re-parsing
# their quotes; the match is on "127.0.0.1:PORT" because "=" ends the search
# string of a !VAR:old=new! substitution.
_SHIM_CMD = """@echo off
rem ciqkit monkeydo shell shim (ciqkit.sim.simport): see _SHIM_CMD there.
set "CIQ_SHIM_ARGS=%*"
setlocal EnableDelayedExpansion
if "!CIQ_SHIM_ARGS:127.0.0.1:%CIQ_SIM_PORT%=!"=="!CIQ_SHIM_ARGS!" exit /b 1
endlocal
"%CIQ_SHIM_PYTHON%" "%CIQ_SHIM_RELAY%" %*
"""


def _shim_path() -> str:
    from ..paths import runtime_dir
    name, text = (("monkeydo-shell-shim.cmd", _SHIM_CMD) if IS_WIN
                  else ("monkeydo-shell-shim.sh", _SHIM))
    path = os.path.join(runtime_dir(), name)
    try:
        with open(path) as f:
            current = f.read()
    except OSError:
        current = None
    if current != text:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:   # text mode: CRLF for cmd.exe on Windows
            f.write(text)
        os.chmod(tmp, 0o700)
        os.replace(tmp, path)
    return path


def _relay_python() -> str:
    """The interpreter to run shellrelay.py with: the base one, not a venv's.
    A venv's Scripts\\python.exe is a launcher that runs the base python as
    its child, so the relay's parent would be that launcher -- which outlives
    the destroyed cmd.exe, and the relay would wait on it forever. The relay
    is stdlib-only, so it needs nothing the venv adds. And the console
    interpreter: pythonw.exe has no stdio of its own to hand the real shell."""
    exe = getattr(sys, "_base_executable", None) or sys.executable
    if not os.path.isfile(exe):
        exe = sys.executable
    head, tail = os.path.split(exe)
    if tail.lower() == "pythonw.exe" and os.path.isfile(os.path.join(head, "python.exe")):
        return os.path.join(head, "python.exe")
    return exe


def _has_monkeydo_class(jar: str) -> bool:
    try:
        with zipfile.ZipFile(jar) as z:
            return MONKEYDO_CLASS.replace(".", "/") + ".class" in z.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def java_exe() -> str | None:
    """The java the SDK's own scripts would run: PATH first, then JAVA_HOME."""
    found = shutil.which("java")
    if found:
        return found
    home = os.environ.get("JAVA_HOME")
    if home:
        cand = os.path.join(home, "bin", "java.exe" if IS_WIN else "java")
        if os.path.isfile(cand):
            return cand
    return None


def _windows_monkeydo(sdk: str, prg: str, device: str, extra) -> tuple[list, str]:
    """(argv, how) for monkeydo on Windows.

    The SDK's bin\\monkeydo.bat is a wrapper around ONE java line, and it is a
    poor thing to drive: its flags are `/t`, `/a`, `/n`, so the `-t` every
    other platform passes falls through to its usage text and the "test run"
    never reaches the simulator; and going through cmd.exe re-parses the .prg
    path with batch quoting rules. So run that java line directly -- the same
    class, jar and shell.exe the batch file would use. Only without java (in
    which case the batch file could not run either) fall back to it, with
    its flags translated.
    """
    java = java_exe()
    jar = os.path.join(sdk, "bin", "monkeybrains.jar")
    shell = os.path.join(sdk, "bin", "shell.exe")
    if java and os.path.isfile(jar) and os.path.isfile(shell):
        return ([java, "-classpath", jar, MONKEYDO_CLASS, "-f", prg, "-d", device,
                 "-s", shell, *extra], "java")
    bat_flags = {"-t": "/t", "-a": "/a", "-n": "/n"}
    return ([os.path.join(sdk, "bin", "monkeydo.bat"), prg, device,
             *[bat_flags.get(a, a) for a in extra]], "monkeydo")


def monkeydo_command(sdk: str, prg: str, device: str, extra=(), st: Status | None = None):
    """(argv, env, how) to run monkeydo on `prg`. `how` is "shim" when the
    probe-skipping shell shim is used, else "monkeydo" (the SDK script, as-is)
    or, on Windows, "java" (the SDK script's own java line -- see
    :func:`_windows_monkeydo`).

    The shim path is the one line the SDK's bin/monkeydo script runs, with our
    shell in place of bin/shell; monkeydo passes -n/-a/-t through verbatim, so
    `extra` does too. Anything unexpected -- no foreign listener, no java, a
    future SDK without the class -- falls back to the SDK's own monkeydo."""
    if IS_WIN:
        plain, how = _windows_monkeydo(sdk, prg, device, extra)
    else:
        plain, how = [os.path.join(sdk, "bin", "monkeydo"), prg, device, *extra], "monkeydo"
    st = st or status()
    jar = os.path.join(sdk, "bin", "monkeybrains.jar")
    shell = os.path.join(sdk, "bin", "shell.exe" if IS_WIN else "shell")
    java = java_exe()
    if (not st.reachable or not st.foreign_before
            or not os.path.exists(shell) or not java
            or not _has_monkeydo_class(jar)):
        return plain, None, how
    env = dict(os.environ, CIQ_SIM_PORT=str(st.port), CIQ_REAL_SHELL=shell)
    if IS_WIN:
        env.update(CIQ_SHIM_PYTHON=_relay_python(),
                   CIQ_SHIM_RELAY=os.path.join(os.path.dirname(__file__), "shellrelay.py"))
    argv = [java, "-classpath", jar, MONKEYDO_CLASS, "-f", prg, "-d", device,
            "-s", _shim_path(), *extra]
    return argv, env, "shim"


def _exec_monkeydo(argv) -> int:
    ap = argparse.ArgumentParser(prog="python -m ciqkit.sim.simport exec-monkeydo")
    ap.add_argument("--sdk", required=True)
    ap.add_argument("prg")
    ap.add_argument("device")
    ap.add_argument("extra", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    extra = a.extra[1:] if a.extra[:1] == ["--"] else a.extra
    cmd, env, how = monkeydo_command(a.sdk, a.prg, a.device, extra)
    if how == "shim":
        print(f"simport: monkeydo via probe-skipping shell shim "
              f"(simulator on {env['CIQ_SIM_PORT']})", file=sys.stderr, flush=True)
    if IS_WIN:
        # Windows has no exec: os.execv* spawns a child and exits at once,
        # so the caller would see our exit code, not monkeydo's.
        return subprocess.call(cmd, env=env)
    os.execvpe(cmd[0], cmd, env if env is not None else os.environ)  # noqa: S606 -- argv list, no shell
    return 127  # not reached


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["exec-monkeydo"]:
        return _exec_monkeydo(argv[1:])
    ap = argparse.ArgumentParser(prog="python -m ciqkit.sim.simport",
                                 description=__doc__.split("\n\n")[0])
    ap.add_argument("--start", action="store_true",
                    help="launch the simulator if none is running")
    ap.add_argument("--wait", type=float, default=20.0,
                    help="seconds to wait for a starting simulator to bind its port")
    ap.add_argument("--sdk", default="", help="SDK directory (default: newest installed)")
    a = ap.parse_args(argv)

    st = status()
    if not st.reachable and not st.sim_pids and a.start:
        from .preflight import detect_sdk
        sdk = a.sdk or detect_sdk()
        if not sdk:
            print("simport: no Connect IQ SDK found to launch the simulator from.")
            return 3
        if st.recent_crash:
            print(f"simport: simulator not running (last crash: {st.recent_crash}); launching it.")
        else:
            print("simport: simulator not running; launching it.")
        launch(sdk)
        st = wait_for_port(max(a.wait, 30.0))
        if st.reachable:
            # The port is bound before the device UI is ready to take an app.
            time.sleep(3)
    elif not st.reachable and st.sim_pids:
        st = wait_for_port(a.wait)
    print("simport: " + describe(st).replace("\n", "\nsimport: "))
    return 0 if st.reachable else 3


if __name__ == "__main__":
    sys.exit(main())
