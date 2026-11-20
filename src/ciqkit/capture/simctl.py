"""Simulator process management: launching, killing and restarting it.

Moved out of ``screenshot_utils`` and re-exported there. Module globals
(``IS_MAC``, ``_sdk``, ``_project``, ``subprocess``, ``time``) and the window
helpers are read through the ``screenshot_utils`` module at call time, so
``init()`` and test monkeypatches of ``su.*`` still take effect here.
"""
import os


def _su():
    from . import screenshot_utils
    return screenshot_utils


def _pkill_best_effort(argv: list, timeout: float = 5) -> None:
    try:
        _su().subprocess.run(["pkill", *argv], capture_output=True, timeout=timeout)
    except _su().subprocess.TimeoutExpired:
        # best effort: a stuck pkill must not stall the restart path
        _su().log.warning("pkill %s timed out after %ss", " ".join(argv), timeout)


# ---------------------------------------------------------------------------
# Kill orphan monkeydo Java processes
# ---------------------------------------------------------------------------

def _kill_orphan_monkeydo():
    """Kill any leftover monkeydo wrapper + Java child that block the push socket.

    On macOS the wrapper is a shell launcher; the actual push is performed by a
    Java process whose command line contains ``MonkeyDoDeux``. A previous
    push that timed out can leave the Java child holding the simulator's push
    socket, which silently rejects later pushes. Kill both.

    Scoped to our own uid (``pkill -u``): the pattern is a command-line
    substring, and on a shared machine another user's process could match it.
    Ours are the only ones we have any business killing.

    Windows: the same match, done in-process -- java.exe processes in our own
    logon session whose command line names MonkeyDoDeux. (This used to shell
    out to ``wmic``, which current Windows 11 no longer ships, so it silently
    did nothing.) With their children: TerminateProcess takes none along, and
    every killed push left its shell.exe running and connected to the
    simulator -- one more per push. Shells already orphaned that way are
    swept too.
    """
    if _su().IS_WIN:
        try:
            _su()._kill_monkeydo_trees_win()
        except Exception:  # noqa: S110
            pass   # best effort: a stale push socket costs a retry, not a capture
    elif _su().IS_MAC:
        for pattern in ("monkeydodeux", "MonkeyDoDeux"):
            _su()._pkill_best_effort(["-u", str(os.getuid()), "-f", pattern])


# What a monkeydo java process starts: the SDK's shell.exe, or simport's shim
# (cmd.exe running shellrelay.py on python.exe). Only these names are followed
# down the tree, so a stale parent pid that happens to match cannot take an
# unrelated program with it.
_MONKEYDO_CHILDREN = ("shell.exe", "cmd.exe", "python.exe")


def _kill_monkeydo_trees_win():
    from .. import _winapi
    mine = _winapi.session_id()
    procs = _winapi.processes()
    alive = {pid for pid, _pp, _n in procs}
    kids: dict = {}
    for pid, ppid, name in procs:
        kids.setdefault(ppid, []).append((pid, name.lower()))
    doomed = []
    for pid, ppid, name in procs:
        name = name.lower()
        if name in ("java.exe", "javaw.exe"):
            if (_winapi.session_id(pid) == mine
                    and "monkeydodeux" in (_winapi.command_line(pid) or "").lower()):
                doomed.append(pid)
                stack = [pid]
                while stack:
                    for child, cname in kids.get(stack.pop(), ()):
                        if cname in _MONKEYDO_CHILDREN:
                            doomed.append(child)
                            stack.append(child)
        elif name == "shell.exe" and ppid not in alive:
            if (_winapi.session_id(pid) == mine
                    and "--transport_args=127.0.0.1:" in (_winapi.command_line(pid) or "")):
                doomed.append(pid)
    for pid in doomed:
        _winapi.terminate(pid)


# ---------------------------------------------------------------------------
# Simulator lifecycle
# ---------------------------------------------------------------------------

def ensure_simulator():
    """Start the CIQ Simulator if not already running.  Returns hwnd.

    When the simulator is already up, returns immediately. When it is not,
    polls every 250 ms (instead of 1 s) for up to 30 s, then waits briefly
    for the window to fully render before returning so the next monkeydo
    push lands on a ready window.
    """
    hwnd = _su().find_sim_window()
    if hwnd:
        return hwnd
    if not _su()._sdk:
        raise _su().SimulatorLaunchError(
            "Connect IQ SDK not found: screenshot_utils.init(sdk, project) has "
            "not been called, or CIQ_HOME points nowhere. Run `ciqkit doctor`.")
    _su().log.debug("Starting simulator...")
    if _su().IS_MAC:
        # macOS: the simulator binary is a wxWidgets/Carbon GUI app and MUST
        # be launched via LaunchServices (`open -a`) so it is registered as a
        # foreground app. Launching the binary directly via subprocess.Popen
        # makes macOS classify it as a background process and the app calls
        # abort() during startup (SIGABRT, procRole=Background in the crash
        # report). It also requires a real local console session — running
        # over Jump Desktop / VNC / Screen Sharing typically still fails with
        # the same abort, because wxWidgets cannot attach to the WindowServer.
        app_bundle = os.path.join(_su()._sdk, "bin", "ConnectIQ.app")
        if not os.path.isdir(app_bundle):
            raise _su().SimulatorLaunchError(
                f"simulator app bundle not found: {app_bundle}")
        _su().subprocess.Popen(["open", "-a", app_bundle],
                         stdout=_su().subprocess.DEVNULL, stderr=_su().subprocess.DEVNULL)
    else:
        sim_candidates = []
        if _su().IS_WIN:
            sim_candidates.append(os.path.join(_su()._sdk, "bin", "simulator.exe"))
        else:
            sim_candidates.append(os.path.join(_su()._sdk, "bin", "simulator"))
        sim_exe = next((p for p in sim_candidates if os.path.exists(p)), None)
        if not sim_exe:
            raise _su().SimulatorLaunchError(
                f"simulator not found in any expected path: {sim_candidates}")
        # Detached (and on Windows console-less and out of the caller's job
        # object): the simulator must outlive the broker that started it, or
        # every idle-exit would take the user's simulator down with it.
        from .._compat import detached_popen
        detached_popen([sim_exe], gui=True, cwd=_su()._project or ".",
                       stdout=_su().subprocess.DEVNULL, stderr=_su().subprocess.DEVNULL,
                       stdin=_su().subprocess.DEVNULL)
    deadline = _su().time.time() + 30.0
    while _su().time.time() < deadline:
        _su().time.sleep(0.25)
        hwnd = _su().find_sim_window()
        if hwnd:
            # Confirm the window is fully realized: poll until a capture
            # succeeds OR a short bounded settle elapses. Faster than the
            # blanket 2 s sleep on machines where the window paints quickly --
            # but only with the preamble off. find_sim_window() just returned
            # this handle, so re-probing it is redundant, and a default capture
            # costs ~2.1 s, which fits into a 2.5 s budget at most once: the
            # loop was slower than the sleep it replaced. At ~0.15 s a look it
            # polls ~15 times and returns as soon as the window paints.
            settle_deadline = _su().time.time() + 2.5
            while _su().time.time() < settle_deadline:
                try:
                    img, _ = _su().capture_window(hwnd, refocus=False,
                                            grab_settle_s=0)
                    if img is not None and img.size[0] > 50:
                        return hwnd
                except Exception:  # noqa: S110
                    pass
                _su().time.sleep(0.2)
            return hwnd
    if _su().IS_MAC:
        raise _su().SimulatorLaunchError(
            "simulator did not start within 30 seconds.\n"
            "  On macOS it needs a real local graphical session; a remote one "
            "(VNC, Screen Sharing, SSH) can abort it.\n"
            "  Check ~/Library/Logs/DiagnosticReports/simulator-*.ips")
    if _su().IS_WIN:
        raise _su().SimulatorLaunchError(
            "simulator did not start within 30 seconds.\n"
            "  It needs an interactive desktop: not a service, a scheduled "
            "task without 'run only when user is logged on', or a locked "
            "session.\n"
            "  Try starting bin\\simulator.exe from the SDK by hand to see "
            "its own error.")
    raise _su().SimulatorLaunchError("simulator did not start within 30 seconds")


def _restart_simulator():
    """Kill and restart the CIQ simulator.  Returns new hwnd.

    This is a hard kill of the running simulator, so anything the loaded app
    held in memory is gone. ciqkit escalates to it only when a push has failed
    to produce a loaded app; if you had the simulator open for your own
    debugging, that session goes with it.

    ``pkill -x`` matches the process name exactly (``simulator``, not Apple's
    ``Simulator``) and ``-u`` limits it to our own uid, so it cannot reach a
    process belonging to anyone else on the machine. On Windows the same
    scoping is our own logon session -- ``taskkill /im simulator.exe``, what
    this used to run, kills every user's simulator when run elevated.
    """
    hwnd = _su().find_sim_window()
    if hwnd:
        _su().log.info("restarting the Connect IQ simulator")
        if _su().IS_WIN:
            from .. import _winapi
            for pid in _winapi.pids_named("simulator.exe"):
                _winapi.terminate(pid)
        elif _su().IS_MAC:
            _su()._pkill_best_effort(["-x", "-u", str(os.getuid()), "simulator"])
        _su().time.sleep(2)
    return _su().ensure_simulator()
