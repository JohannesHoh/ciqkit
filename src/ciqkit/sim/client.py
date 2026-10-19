"""Client library + CLI for the simulator broker.

Connects to the broker daemon over its control channel (a unix socket, or
authenticated loopback TCP on Windows -- see :mod:`ciqkit.sim.transport`),
auto-spawning it
(ssh-agent style) if it is not running. The library API mirrors the CLI, so a
capture script or an MCP shim can call it directly.

CLI (see ``ciqkit sim --help``)::

  ciqkit sim status
  ciqkit sim shot   NAME  [--project DIR] [--prg PATH] [--anchor TEXT] [--urgent]
  ciqkit sim record NAME  [--project DIR] [--duration 8]
  ciqkit sim nav    --to SCREEN --steps '<json>' [--anchor TEXT]
  ciqkit sim shots  (--all | --screen NAME ...) [--out-dir DIR]   # [[screens]] in ciqkit.toml
  ciqkit sim test   [--prg build/MyApp-test.prg] [--priority batch]
  ciqkit sim wait   JOB_ID [--timeout 600]
  ciqkit sim bump   JOB_ID [--priority urgent]
  ciqkit sim cancel JOB_ID
  ciqkit sim restore
  ciqkit sim shutdown
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

from . import jobs
from . import protocol as proto
from . import seedjournal
try:
    from ..leases import ENV_KEY as LEASE_ENV_KEY
except ImportError:          # nothing to strip if leases cannot load here
    LEASE_ENV_KEY = "CIQKIT_LEASES_HELD"
from . import transport
from .._compat import (IS_WIN, detached_popen, hidden_console_kwargs,
                       try_lock, unlock)
from .protocol import DEFAULT_PLAYBACK_MS, Job, ChunkKind, Priority

SPAWN_WAIT_S = float(os.environ.get("CIQSIM_SPAWN_WAIT_S", "20"))


class BrokerNotRunning(OSError):
    """Nothing is listening on the control socket.

    A distinct type on purpose: "no daemon" and "the daemon died mid-reply" are
    both OSError, and reporting the second as the first sends the user looking
    for a broker that was there a moment ago.
    """


class Client:
    def __init__(self, sock_path: str | None = None, autospawn: bool = True):
        self.sock_path = sock_path or proto.socket_path()
        self.autospawn = autospawn

    def _connect(self) -> socket.socket:
        try:
            return transport.connect(self.sock_path, timeout=5.0)
        except OSError as exc:
            if not self.autospawn:
                if isinstance(exc, (FileNotFoundError, ConnectionRefusedError)):
                    raise BrokerNotRunning(str(exc)) from None
                raise
            spawn_daemon(self.sock_path)
            # A cold daemon imports numpy and Pillow before it listens; on a
            # Windows box with real-time AV scanning every freshly opened .pyd
            # that alone has been measured at several seconds.
            deadline = time.time() + SPAWN_WAIT_S
            while time.time() < deadline:
                try:
                    return transport.connect(self.sock_path, timeout=5.0)
                except OSError:
                    time.sleep(0.1)
            tried = ", ".join(broker_python_candidates())
            raise RuntimeError(
                "the ciqkit broker did not start.\n"
                "  The usual cause is a base install: driving the simulator "
                "needs Pillow, numpy and pyautogui.\n"
                "    pip install \"ciqkit[sim]\"\n"
                "  None of these interpreters could import them: "
                f"{tried}\n"
                "  Point CIQKIT_PYTHON at one that can, run `ciqkit doctor` "
                "for the full picture, or read the log at "
                f"{os.path.join(proto.runtime_dir(), 'brokerd.log')}") from None

    def _rpc(self, req: dict, read_timeout: float = 630.0) -> dict:
        s = self._connect()
        try:
            s.settimeout(read_timeout)
            s.sendall(proto.dumps(req))
            f = s.makefile("rb")
            line = f.readline()
            return proto.loads(line) if line else {"ok": False, "error": "no response"}
        finally:
            s.close()

    # library API
    def ping(self) -> dict:
        return self._rpc({"op": proto.Op.PING})

    def submit(self, job: Job) -> str:
        r = self._rpc({"op": proto.Op.SUBMIT, "job": job.to_dict()})
        if not r.get("ok") and r.get("error") == proto.ERR_SHUTTING_DOWN:
            # Reached a broker on its way out (e.g. right after `sim shutdown`).
            # Let it finish exiting, then submit again: the connect autospawns
            # a fresh one.
            self.wait_gone()
            r = self._rpc({"op": proto.Op.SUBMIT, "job": job.to_dict()})
        if not r.get("ok"):
            raise RuntimeError(r.get("error", "submit failed"))
        return r["job_id"]

    def wait(self, job_id: str, timeout_s: float = 600.0) -> dict:
        return self._rpc({"op": proto.Op.WAIT, "job_id": job_id,
                          "timeout_s": timeout_s}, read_timeout=timeout_s + 30)

    def submit_wait(self, job: Job, timeout_s: float | None = None) -> dict:
        """Submit and wait. ``timeout_s`` defaults to :func:`jobs.wait_budget`,
        so a caller only passes one to override it (the CLI's ``--max``)."""
        jid = self.submit(job)
        return self.wait(jid, timeout_s or jobs.wait_budget(job))

    def status(self) -> dict:
        return self._rpc({"op": proto.Op.STATUS})

    def bump(self, job_id: str, priority: int = Priority.URGENT) -> dict:
        return self._rpc({"op": proto.Op.BUMP, "job_id": job_id, "priority": priority})

    def cancel(self, job_id: str) -> dict:
        return self._rpc({"op": proto.Op.CANCEL, "job_id": job_id})

    def shutdown(self, wait_s: float = 30.0) -> dict:
        """Stop the broker and return once it has actually exited.

        The SHUTDOWN reply comes back while the daemon is still running: its
        accept loop polls a stop flag once a second, then it restores seeds and
        exits. Returning at the reply let the next command reach the dying
        broker -- its job vanished with it and the WAIT, sent to the freshly
        spawned one, came back "no such job". ``stopped`` says whether it
        exited within ``wait_s``.
        """
        r = self._rpc({"op": proto.Op.SHUTDOWN})
        if r.get("ok"):
            r["stopped"] = self.wait_gone(wait_s)
        return r

    def wait_gone(self, timeout_s: float = 30.0) -> bool:
        """Block until no broker holds the daemon-singleton lock beside the
        socket. That flock is released by the kernel only when the broker
        process has exited, so unlike the socket file it cannot be stale or
        early. True when it is free, False on timeout."""
        lock_path = self.sock_path + ".lock"
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fd = os.open(lock_path, os.O_RDWR | getattr(os, "O_BINARY", 0))
            except FileNotFoundError:
                return True               # no broker has ever run here
            try:
                if try_lock(fd):
                    unlock(fd)
                    return True
            except OSError:
                pass
            finally:
                os.close(fd)
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)


# The broker needs the sim extras (Pillow, numpy, pyautogui); the process that
# *calls* the client often does not have them. An MCP shim, a build script or a
# bare `python3 -m` run can import ciqkit fine over PYTHONPATH and still be a
# base interpreter — spawning the daemon with sys.executable then fails with
# "the ciqkit broker did not start", which reads like missing deps rather than
# the wrong interpreter. So pick an interpreter that can actually run it.
_BROKER_PROBE = (
    "import PIL, numpy, pyautogui, ciqkit.sim.broker"  # noqa: F401
)


def _can_run_broker(python: str) -> bool:
    try:
        r = subprocess.run([python, "-c", _BROKER_PROBE],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL, timeout=30,
                           **hidden_console_kwargs())
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


# Where a venv keeps its interpreter: bin/python on POSIX, Scripts\python.exe
# on Windows.
_VENV_PYTHON = ("Scripts", "python.exe") if IS_WIN else ("bin", "python")


def _venvs_above(start: str):
    """Yield the `.venv` interpreter for `start` and each parent."""
    d = os.path.abspath(start)
    seen = set()
    while d and d not in seen:
        seen.add(d)
        cand = os.path.join(d, ".venv", *_VENV_PYTHON)
        if os.path.exists(cand):
            yield cand
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent


def broker_python_candidates() -> list[str]:
    """Interpreters to try for the daemon, best first."""
    out = []

    def add(p):
        if p and p not in out:
            out.append(p)

    add(os.environ.get("CIQKIT_PYTHON"))
    add(sys.executable)
    # The venv ciqkit itself is installed into, when this module was imported
    # from one.
    add(os.path.join(sys.prefix, *_VENV_PYTHON))
    # A .venv beside the caller's project or beside the ciqkit checkout — the
    # editable-install layout every consumer of this package uses.
    for start in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        for cand in _venvs_above(start):
            add(cand)
    return out


def _cached_broker_python_path() -> str:
    return os.path.join(proto.runtime_dir(), "broker-python")


def resolve_broker_python() -> str | None:
    """First candidate that can import the broker AND the sim extras.

    The winner is cached in the runtime dir so the probe (a subprocess import
    per candidate) runs once per machine-boot rather than once per spawn.
    """
    cache = _cached_broker_python_path()
    try:
        with open(cache) as f:
            cached = f.read().strip()
        if cached and os.path.exists(cached) and _can_run_broker(cached):
            return cached
    except OSError:
        pass

    for cand in broker_python_candidates():
        if _can_run_broker(cand):
            try:
                with open(cache, "w") as f:
                    f.write(cand)
            except OSError:
                pass
            return cand
    return None


def spawn_daemon(sock_path: str | None = None):
    """Start the broker in the background. Idempotent: a second daemon sees the
    live socket and exits immediately."""
    sock_path = sock_path or proto.socket_path()
    log_path = os.path.join(proto.runtime_dir(), "brokerd.log")
    env = dict(os.environ, CIQSIM_SOCK=sock_path)
    # The daemon outlives whoever spawned it. If that caller was running under
    # `ciqkit lease sim -- ...`, the inherited "already held" marker would make
    # every one of the daemon's own `sim` lease acquisitions a no-op for its
    # whole life -- long after the real holder exited -- and an unmigrated
    # script could then drive the simulator alongside it.
    env.pop(LEASE_ENV_KEY, None)
    python = resolve_broker_python()
    logf = open(log_path, "a")
    if python is None:
        # Nothing viable found. Still try sys.executable so the daemon log
        # carries the real ImportError for whoever reads it.
        python = sys.executable
        logf.write("[client] no interpreter with the sim extras found; "
                   f"falling back to {python}\n")
        logf.flush()
    # Detached the way each platform needs (see _compat.detached_popen): on
    # Windows that means a hidden console of its own, so the daemon survives
    # the terminal that started it and its builds and pushes do not flash
    # console windows over the simulator mid-capture.
    detached_popen([python, "-m", "ciqkit.sim.broker"], env=env,
                   stdout=logf, stderr=logf, stdin=subprocess.DEVNULL)


# ── CLI ───────────────────────────────────────────────────────────────────────
_PRIO = {"urgent": Priority.URGENT, "high": Priority.HIGH,
         "normal": Priority.NORMAL, "batch": Priority.BATCH}


def _prio(args) -> int:
    if getattr(args, "urgent", False):
        return Priority.URGENT
    return _PRIO.get(getattr(args, "priority", "normal") or "normal", Priority.NORMAL)


def _project_dir(args) -> str:
    """The Connect IQ project directory this job targets.

    Defaults to the current directory, so the common case is just running the
    command from inside your project. Validated up front: pointing at the wrong
    directory otherwise surfaces much later as a confusing build error.
    """
    d = os.path.abspath(getattr(args, "project", None) or ".")
    if not os.path.isdir(d):
        raise SystemExit(f"error: --project directory does not exist: {d}")
    if not os.path.isfile(os.path.join(d, "manifest.xml")):
        raise SystemExit(
            f"error: {d} does not look like a Connect IQ project "
            f"(no manifest.xml). Pass --project <dir>, or run from inside the "
            f"project.")
    return d


def _project_name(project_dir: str) -> str:
    """Short label for status output; also the broker's warm-app key."""
    return os.path.basename(os.path.normpath(project_dir)) or "project"


def _abs(project_dir, path):
    if not path:
        return path
    # normpath: on Windows `--prg bin/App.prg` would otherwise reach monkeydo
    # as `C:\proj\bin/App.prg`, and the storage journal keys on that string.
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(project_dir, path))


def build_parser(prog="ciqkit sim"):
    import argparse
    p = argparse.ArgumentParser(
        prog=prog,
        description="Drive the Connect IQ Simulator through the ciqkit broker.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--project", default=None, metavar="DIR",
                        help="Connect IQ project directory (default: cwd)")
        sp.add_argument("--device", default=None,
                        help="simulator device id (default: the first "
                             "manifest.xml product installed in the SDK, "
                             "else venu3)")
        sp.add_argument("--prg", default=None, metavar="PATH",
                        help="already-built .prg, relative to the project dir. "
                             "Omit to run the project's build command "
                             "(see CIQKIT_BUILD_CMD).")
        sp.add_argument("--storage", default=None, metavar="fresh|keep|DIR",
                        help="app storage the cold push starts from: fresh "
                             "(default: empty -- never another run's "
                             "leftovers), keep (inherit), or a fixture dir. "
                             "Journaled either way; see `ciqkit storage`.")
        sp.add_argument("--fit", default=None, metavar="FILE",
                        help="after the verified load, start the simulator's FIT "
                             "playback of this .fit/.gpx file (Simulation > "
                             "Activity Data) and verify it is playing. macOS only.")
        sp.add_argument("--priority", choices=list(_PRIO))
        sp.add_argument("--urgent", action="store_true",
                        help="shorthand for --priority urgent")
        sp.add_argument("--force", action="store_true",
                        help="re-push even if the same project is already "
                             "loaded. Use after rebuilding the same .prg: the "
                             "warm-load shortcut would otherwise re-shoot the "
                             "PREVIOUS build's screen.")
        sp.add_argument("--max", type=float, default=None, metavar="SECONDS",
                        help="hard timeout for this job")

    sp = sub.add_parser("shot", help="capture one screenshot")
    common(sp)
    sp.add_argument("name", help="output file name (without .png)")
    sp.add_argument("--anchor", action="append", metavar="TEXT",
                    help="text that must appear on screen (OCR); repeatable")
    sp.add_argument("--no-crop", action="store_true",
                    help="save the whole simulator window, uncropped")
    sp.add_argument("--native", action="store_true",
                    help="raw cropped watch face WITHOUT the body+band frame "
                         "(default is framed, suitable for store listings)")
    sp.add_argument("--out-dir", default=None,
                    help="where to write the PNG (default: a private ciqkit dir)")
    sp.add_argument("--seed", action="append", default=[], metavar="NAME=VALUE",
                    help="patch a constant in source/Constants.mc for this build "
                         "and restore it afterwards; repeatable")
    sp.add_argument("--on-marker", default=None, metavar="TEXT",
                    help="take the shot when the app's console prints TEXT (a "
                         "System.println line) instead of right after the load. "
                         "Always a cold load.")
    sp.add_argument("--marker-timeout", type=float, default=None, metavar="SECONDS",
                    help="how long to wait for the marker (default: 20)")

    sp = sub.add_parser(
        "shot-on",
        help="one load, then a shot each time the app's console prints a marker")
    common(sp)
    sp.add_argument("--marker", action="append", default=[], required=True,
                    metavar="TEXT=NAME",
                    help="shoot NAME.png when the console prints TEXT; repeat "
                         "for several, taken in the order the app prints them")
    sp.add_argument("--marker-timeout", type=float, default=None, metavar="SECONDS",
                    help="how long to wait for each marker (default: 20)")
    sp.add_argument("--no-crop", action="store_true",
                    help="save the whole simulator window, uncropped")
    sp.add_argument("--native", action="store_true",
                    help="raw cropped watch face WITHOUT the body+band frame")
    sp.add_argument("--out-dir", default=None,
                    help="where to write the PNGs (default: a private ciqkit dir)")

    sp = sub.add_parser(
        "shots",
        help="capture the named [[screens]] from ciqkit.toml, one PNG each")
    common(sp)
    sp.add_argument("--all", action="store_true", help="every screen in ciqkit.toml")
    sp.add_argument("--screen", action="append", default=[], metavar="NAME",
                    help="one screen by name; repeatable")
    sp.add_argument("--no-crop", action="store_true",
                    help="save the whole simulator window, uncropped")
    sp.add_argument("--native", action="store_true",
                    help="raw cropped watch face WITHOUT the body+band frame")
    sp.add_argument("--out-dir", default=None,
                    help="where to write <name>.png (default: a private ciqkit dir)")

    sp = sub.add_parser("record", help="record the screen to an animated GIF")
    common(sp)
    sp.add_argument("name")
    sp.add_argument("--duration", type=float, default=8.0, help="seconds (default: 8)")
    sp.add_argument("--interval", type=int, default=200,
                    help="target ms between frames (default: 200; the real rate "
                         "is capture-bound and floors at ~220 ms/frame)")
    sp.add_argument("--playback-ms", type=int, default=DEFAULT_PLAYBACK_MS,
                    help="median frame duration in the written GIF (default: "
                         f"{DEFAULT_PLAYBACK_MS}, ~2 fps). This is a CEILING: "
                         "the track is only ever sped UP to it, so a clip "
                         "captured at the usual ~4.6 fps plays at its real rate "
                         "and only a degraded one gets compressed. Frame-gap "
                         "ratios are preserved. 0 = raw wall-clock timing")
    sp.add_argument("--native", action="store_true",
                    help="tight cropped face per frame WITHOUT the watch "
                         "body+band frame (default is framed like `shot`: the "
                         "complete watch on the store background)")
    sp.add_argument("--no-crop", action="store_true",
                    help="raw simulator window frames: no framing, no crop")
    sp.add_argument("--out-dir", default=None)
    sp.add_argument("--no-frames", action="store_true",
                    help="do not keep the raw frame PNGs")
    sp.add_argument("--seed", action="append", default=[], metavar="NAME=VALUE",
                    help="patch a constant in source/Constants.mc for this build "
                         "and restore it afterwards")

    sp = sub.add_parser("nav", help="run nav steps, then verify the target screen")
    common(sp)
    sp.add_argument("--to", required=True, metavar="SCREEN",
                    help="label for the screen you expect to reach")
    sp.add_argument("--steps", default="[]",
                    help='JSON list of gestures, e.g. \'[{"kind":"tap","x":0.5,"y":0.8}]\'')
    sp.add_argument("--anchor", action="append", metavar="TEXT")

    sp = sub.add_parser("test", help="run a Connect IQ unit-test build")
    common(sp)

    sub.add_parser(
        "restore",
        help="put back files a seed left patched when its process was killed")

    sub.add_parser("status", help="show the broker queue")
    sp = sub.add_parser(
        "wait",
        help="re-attach to a running job and wait for its result")
    sp.add_argument("job_id")
    sp.add_argument("--timeout", type=float, default=600.0, metavar="SECONDS")
    sp = sub.add_parser("bump", help="raise a queued job's priority")
    sp.add_argument("job_id")
    sp.add_argument("--priority", choices=list(_PRIO), default="urgent")
    sp = sub.add_parser("cancel", help="cancel a job")
    sp.add_argument("job_id")
    sp = sub.add_parser(
        "phone", help="set the simulated phone link (Settings > Connection Type "
                      "> BLE); DeviceSettings.phoneConnected follows it")
    sp.add_argument("state", choices=["connected", "disconnected"])
    sp.add_argument("--priority", choices=list(_PRIO))
    sp.add_argument("--urgent", action="store_true")
    sp = sub.add_parser(
        "close-aux", help="close the simulator's auxiliary windows (the "
                          "openWebPage web view, panels) and stray alerts")
    sp.add_argument("--priority", choices=list(_PRIO))
    sp.add_argument("--urgent", action="store_true")
    sub.add_parser("shutdown", help="stop the broker daemon")
    sub.add_parser("ping", help="check whether the broker is alive")
    return p


def _refuse_seed_without_build(args):
    """Exit before any work when --seed and --prg are combined (see jobs)."""
    try:
        jobs.refuse_seed_with_prg(getattr(args, "seed", None), getattr(args, "prg", None),
                                  seed_name="--seed", prg_name="--prg")
    except jobs.JobError as exc:
        raise SystemExit(f"error: {exc}") from exc


def _parse_kv(pairs, what):
    out = {}
    for kv in pairs:
        k, sep, v = kv.partition("=")
        if not sep or not k.strip():
            raise SystemExit(f"error: {what} must be NAME=VALUE, got {kv!r}")
        out[k.strip()] = v.strip()
    return out


# Commands that only look at, or wind down, an existing broker. Starting a
# daemon to answer them is worse than useless: `ping` could then never report
# "not running", and `status` would boot a broker that then idles for ten
# minutes just to tell you there was nothing to see.
_READ_ONLY = {"status", "ping", "wait", "bump", "cancel", "shutdown"}


def main(argv=None):
    args = build_parser().parse_args(argv)
    c = Client(autospawn=args.cmd not in _READ_ONLY)

    if args.cmd in _READ_ONLY:
        try:
            return _read_only(c, args)
        except BrokerNotRunning:
            return _not_running(args.cmd)
    if args.cmd == "restore":
        return _restore()

    if args.cmd in ("phone", "close-aux"):
        return _sim_op(c, args)

    proj = _project_dir(args)
    name = _project_name(proj)
    from ..config import ConfigError, load_config
    try:
        cfg = load_config(proj)
        from ..build import resolve_device
        args.device = resolve_device(proj, args.device)
    except ConfigError as exc:
        raise SystemExit(f"error: {exc}") from exc
    _refuse_seed_without_build(args)
    try:
        if args.cmd == "shots":
            return _shots(c, args, proj, name, cfg)
        return _run_job_command(c, args, proj, name)
    except jobs.JobError as exc:     # a builder or make_job refused the job locally
        raise SystemExit(f"error: {exc}") from exc


def _sim_op(c: Client, args) -> int:
    """`sim phone` / `sim close-aux`: simulator-level ops, no app involved."""
    if args.cmd == "phone":
        steps = [{"kind": "phone", "state": args.state}]
    else:
        steps = [{"kind": "close_aux"}]
    args.device = getattr(args, "device", None) or "venu3"
    job = _mkjob(args, "(simulator)", jobs.op_chunks(steps), args.cmd)
    r = c.submit_wait(job)
    print(json.dumps(r, indent=2))
    return 0 if r.get("ok") and _op_steps_ok(r) else 1


def _op_steps_ok(r) -> bool:
    res = (r.get("job") or r).get("results") or []
    for x in res:
        if isinstance(x, dict) and isinstance(x.get("steps"), list):
            if not all(st.get("ok") for st in x["steps"]):
                return False
    return True


def _run_job_command(c: Client, args, proj, name) -> int:
    if args.cmd == "shot":
        job = _mkjob_screen(args, proj, name, args.name, anchor=args.anchor,
                            seed=_parse_kv(args.seed, "--seed") if args.seed else {},
                            out_dir=args.out_dir)
        r = c.submit_wait(job, timeout_s=args.max)
        print(json.dumps(r, indent=2))
        if not r.get("ok"):
            _hint_if_still_running(r)
            return 1
        return 0 if _anchor_ok(r) else 3

    if args.cmd == "shot-on":
        shots = []
        for spec in args.marker:
            text, sep, nm = spec.rpartition("=")
            if not sep or not text or not nm:
                raise SystemExit(f"error: --marker must be TEXT=NAME, got {spec!r}")
            shots.append({"marker": text, "name": nm})
        chunks = jobs.marker_shot_chunks(
            project=proj, device=args.device, shots=shots, prg=args.prg,
            storage=getattr(args, "storage", None), out_dir=args.out_dir,
            crop=not args.no_crop, frame=not (args.native or args.no_crop),
            fit=args.fit, timeout_s=args.marker_timeout)
        job = _mkjob(args, name, chunks, f"shot-on {len(shots)}")
        r = c.submit_wait(job, timeout_s=args.max)
        print(json.dumps(r, indent=2))
        if not r.get("ok"):
            _hint_if_still_running(r)
            return 1
        return 0 if _anchor_ok(r) else 3

    if args.cmd == "record":
        chunks = jobs.record_chunks(
            project=proj, device=args.device, name=args.name,
            duration_s=args.duration, interval_ms=args.interval,
            playback_ms=args.playback_ms, prg=args.prg,
            seed=_parse_kv(args.seed, "--seed") if args.seed else None,
            storage=getattr(args, "storage", None), out_dir=args.out_dir,
            crop=not args.no_crop, frame=not (args.native or args.no_crop),
            keep_frames=not args.no_frames, force=getattr(args, "force", False),
            fit=args.fit)
        job = _mkjob(args, name, chunks, f"record {args.name}")
        r = c.submit_wait(job, timeout_s=args.max)
        print(json.dumps(r, indent=2))
        if r.get("ok"):
            return 0
        _hint_if_still_running(r)
        return 1

    if args.cmd == "nav":
        try:
            steps = json.loads(args.steps)
        except ValueError as exc:
            raise SystemExit(f"error: --steps is not valid JSON: {exc}") from exc
        if not isinstance(steps, list):
            raise SystemExit("error: --steps must be a JSON list of gesture objects")
        chunks = jobs.nav_chunks(
            project=proj, device=args.device, to=args.to, steps=steps,
            prg=args.prg, anchor=args.anchor, force=getattr(args, "force", False),
            storage=getattr(args, "storage", None), fit=args.fit)
        job = _mkjob(args, name, chunks, f"nav {args.to}")
        r = c.submit_wait(job, timeout_s=args.max)
        print(json.dumps(r, indent=2))
        if not r.get("ok"):
            _hint_if_still_running(r)
            return 1
        return _nav_exit(r)

    if args.cmd == "test":
        job = _mkjob(args, name,
                     jobs.test_chunks(project=proj, device=args.device, prg=args.prg,
                                      max_s=args.max),
                     f"test {name}", default_prio=Priority.BATCH)
        r = c.submit_wait(job, timeout_s=args.max)
        print(json.dumps(r, indent=2))
        if not r.get("ok"):
            _hint_if_still_running(r)
            return 1
        return _tests_exit(r)

    return 2


def _read_only(c: Client, args) -> int:
    """Commands that talk to an already-running broker. A missing daemon
    propagates as BrokerNotRunning and is reported by _not_running; every other
    socket failure is a real error and must not be disguised as one."""
    if args.cmd == "status":
        _print_status(c.status())
        return 0
    if args.cmd == "ping":
        print(json.dumps(c.ping()))
        return 0
    if args.cmd == "wait":
        r = c.wait(args.job_id, args.timeout)
        print(json.dumps(r, indent=2))
        if not r.get("ok"):
            _hint_if_still_running(r)
            return 1
        return 0 if _anchor_ok(r) else 3
    if args.cmd in ("bump", "cancel"):
        # Exit on what the broker actually said. `no such job` -- which
        # retention reaping now makes routine for anything older than 15
        # minutes -- must not exit 0 while the JSON on stdout says it failed.
        r = (c.bump(args.job_id, _PRIO[args.priority]) if args.cmd == "bump"
             else c.cancel(args.job_id))
        print(json.dumps(r))
        return 0 if r.get("ok") else 1
    if args.cmd == "shutdown":
        r = c.shutdown()
        # Return once it has actually gone, not when it has agreed to: the
        # next command would otherwise reach the old broker mid-exit.
        deadline = time.time() + 15.0
        while r.get("ok") and time.time() < deadline:
            try:
                c.ping()
            except (BrokerNotRunning, OSError):
                break
            time.sleep(0.2)
        print(json.dumps(r))
        return 0
    raise AssertionError(f"not a read-only command: {args.cmd}")


_NOT_RUNNING = ("no broker is running "
                "(it starts on demand when you submit a shot, record or test)")


def _not_running(cmd: str) -> int:
    """No broker is listening. Whether that is an answer or an error depends on
    what was asked: `status` and `shutdown` got what they wanted, the rest did
    not. The JSON-speaking commands keep stdout machine-readable and put the
    sentence on stderr."""
    if cmd == "status":
        print(_NOT_RUNNING)
        return 0
    if cmd in ("shutdown", "ping"):
        print(json.dumps({"ok": cmd == "shutdown", "running": False}))
        print(_NOT_RUNNING, file=sys.stderr)
        return 0 if cmd == "shutdown" else 1
    verb = {"wait": "wait for", "cancel": "cancel", "bump": "bump"}[cmd]
    print(f"error: {_NOT_RUNNING}, so there is no job to {verb}.",
          file=sys.stderr)
    return 1


def _restore() -> int:
    """Put back files a seed left patched when its process died uncatchably.

    Purely local: it reads ciqkit's own journal, so it works even when no
    broker is running. A journal whose owner is still alive belongs to a live
    job, which will restore from memory when it ends — we refuse rather than
    yank the tree out from under it.
    """
    j = seedjournal.pending()
    if not j:
        print("nothing to restore — no seed is pending")
        return 0
    if seedjournal.owner_alive():
        print(f"a seed is active and its owner (pid {j['pid']}) is still "
              f"running; it will restore when that job ends. Use "
              f"`ciqkit sim status` and `ciqkit sim cancel <id>` if it is stuck.",
              file=sys.stderr)
        return 1
    for p in seedjournal.recover():
        print(f"restored {p}")
    stale = seedjournal.stale()
    if not stale:
        return 0
    print("\nNOT restored — these files changed after ciqkit patched them, so "
          "writing the backup back would discard that change:", file=sys.stderr)
    for e in stale:
        print(f"  {e['path']}\n    original bytes kept at: {e['backup']}",
              file=sys.stderr)
    print("  Diff the two and copy across whatever you still want.", file=sys.stderr)
    return 1


def _hint_if_still_running(r: dict):
    """A wait timeout is not a cancellation: the job keeps running and will
    still write its output. Say so, and say how to collect it -- otherwise the
    printed job id is a dead end. Goes to stderr; stdout carries the JSON."""
    if r.get("error") != "wait timeout":
        return
    job = r.get("job") or {}
    print(f"\nnote: job {job.get('id')} is still {job.get('state')} — it keeps "
          f"running and will still produce its output.\n"
          f"  re-attach with: ciqkit sim wait {job.get('id')}", file=sys.stderr)


def _nav_exit(result) -> int:
    """Exit code for `nav`, from the reach step's own verdict.

    The job completing only means the chunks ran. `nav` exists to answer "did I
    reach that screen", so reporting 0 while the reach said otherwise makes the
    command worthless -- and quiet about it. Codes match `shot`: 3 when an
    anchor was explicitly contradicted, 1 when a gesture did not land.
    """
    for step in result.get("job", {}).get("results", []):
        if step.get("kind") != ChunkKind.REACH:
            continue
        res = step.get("result") or {}
        if res.get("anchor") is False:
            print(f"error: ran the steps but the target screen "
                  f"{res.get('screen')!r} did not show the expected text",
                  file=sys.stderr)
            return 3
        if not res.get("ok"):
            print(f"error: {res.get('error', 'nav did not reach the screen')}",
                  file=sys.stderr)
            return 1
    return 0


def _tests_exit(result) -> int:
    """Exit code for `test`, from the test run's own verdict.

    The job finishing only means the chunk ran. A run with no verdict --
    "Unable to connect to simulator.", a test build that crashed on load,
    zero tests -- comes back as a DONE job carrying ``ok: false``, and
    exiting 0 on the job alone called every one of those green. Scripts and
    CI gate on this code, so it has to be the tests' answer.
    """
    for step in result.get("job", {}).get("results", []):
        if step.get("kind") != ChunkKind.TEST_BATCH:
            continue
        res = step.get("result") or {}
        if not res.get("ok"):
            why = res.get("reason") or (f"passed={res.get('passed')} "
                                        f"failed={res.get('failed')} "
                                        f"errors={res.get('errors')}")
            print(f"error: tests did not pass ({why})", file=sys.stderr)
            return 1
    return 0


def _anchor_ok(result) -> bool:
    """False only when OCR ran and the expected text was NOT on screen.

    `None` means OCR is unavailable or returned nothing, in which case the
    anchor is a skipped check rather than a failed one -- failing there would
    break every machine without an OCR backend installed.
    """
    for step in result.get("job", {}).get("results", []):
        if step.get("kind") == ChunkKind.SHOT:
            if (step.get("result") or {}).get("anchor") is False:
                return False
    return True


def _mkjob_screen(args, proj, app, name, *, anchor=None, seed=None, steps=None,
                  storage=None, out_dir=None, force=None):
    chunks = jobs.screen_chunks(
        project=proj, device=args.device, name=name, prg=args.prg, anchor=anchor,
        seed=seed, steps=steps, storage=storage or getattr(args, "storage", None),
        out_dir=out_dir, crop=not args.no_crop, frame=not args.native,
        force=force if force is not None else getattr(args, "force", False),
        fit=getattr(args, "fit", None), marker=getattr(args, "on_marker", None),
        marker_timeout_s=getattr(args, "marker_timeout", None))
    return _mkjob(args, app, chunks, f"shot {name}")


def _shots(c: Client, args, proj, app, cfg) -> int:
    """``sim shots``: one broker job per named screen, strictly in order.

    Each screen is its own job so the broker can still preempt between them,
    and every one is force-loaded: a warm app would otherwise carry the
    previous screen's navigation, seed or storage into the next.
    """
    if bool(args.all) == bool(args.screen):
        raise SystemExit("error: pass exactly one of --all or --screen NAME")
    if not cfg.screens:
        raise SystemExit(f"error: no [[screens]] defined in "
                         f"{cfg.path or os.path.join(proj, 'ciqkit.toml')}")
    if args.all:
        picked = list(cfg.screens)
    else:
        picked = []
        for n in args.screen:
            sc = cfg.screen(n)
            if sc is None:
                raise SystemExit(f"error: no screen {n!r} in {cfg.path}; defined: "
                                 f"{', '.join(s.name for s in cfg.screens)}")
            if sc not in picked:
                picked.append(sc)
    if args.prg and any(sc.seed for sc in picked):
        jobs.refuse_seed_with_prg(True, args.prg, seed_name="--seed", prg_name="--prg")
    summary, failed = [], False
    for sc in picked:
        # --storage on the command line outranks the screen's own; a fixture
        # dir in the toml is relative to the project, not to the shell's cwd.
        st = args.storage or sc.storage
        if st and not args.storage and st not in ("fresh", "keep"):
            st = _abs(proj, os.path.expanduser(st))
        job = _mkjob_screen(args, proj, app, sc.name, anchor=sc.anchor or None,
                            seed=sc.seed, steps=sc.steps, storage=st,
                            out_dir=args.out_dir, force=True)
        try:
            r = c.submit_wait(job, timeout_s=args.max)
        except (RuntimeError, OSError) as exc:
            r = {"ok": False, "error": str(exc)}
        row = screen_summary(sc.name, r)
        failed = failed or not row["ok"]
        summary.append(row)
        print(json.dumps(row), flush=True)
        if not r.get("ok"):
            _hint_if_still_running(r)
    return 1 if failed else 0


def screen_summary(name: str, r: dict) -> dict:
    """Per-screen verdict from a finished shot job (shared with the MCP tool)."""
    job = r.get("job") or {}
    row = {"screen": name, "ok": bool(r.get("ok")), "path": None,
           "job_id": job.get("id")}
    err = job.get("error") or (r.get("error") if not r.get("ok") else None)
    for step in job.get("results", []):
        res = step.get("result") or {}
        if step.get("kind") == ChunkKind.SHOT:
            row["path"] = res.get("path")
            row["anchor"] = res.get("anchor")
            if res.get("anchor") is False:
                row["ok"] = False
                err = err or "expected anchor text was not on screen"
        elif step.get("kind") == ChunkKind.REACH:
            if res.get("anchor") is False:
                row["ok"] = False
                err = err or "steps ran but the screen did not show the expected text"
            elif not res.get("ok"):
                row["ok"] = False
                err = err or res.get("error", "steps did not reach the screen")
    if not row["ok"]:
        row["error"] = err or "failed"
    return row


def _mkjob(args, app, chunks, label, default_prio=None):
    prio = _prio(args) if (args.urgent or args.priority) else (default_prio or Priority.NORMAL)
    return jobs.make_job(app=app, device=args.device, chunks=chunks, priority=prio,
                         owner=os.environ.get("CIQKIT_SESSION_LABEL", ""), label=label)


def _print_status(st):
    if not st.get("ok"):
        print(json.dumps(st))
        return
    print(f"loaded: {st.get('loaded')}   current job: {st.get('current')}")
    rows = sorted(st.get("jobs", []), key=lambda j: (j["state"] != "running", j["eff"]))
    if not rows:
        print("  (no jobs)")
        return
    print(f"  {'ID':12} {'STATE':9} {'PRIO':5} {'EFF':6} {'CHUNK':7} {'ETA':6} LABEL")
    for j in rows:
        print(f"  {j['id']:12} {j['state']:9} {j['priority']:<5} {j['eff']:<6} "
              f"{str(j['chunk'])+'/'+str(j['chunks']):7} {str(j['eta_s'])+'s':6} {j['label']}")


if __name__ == "__main__":
    sys.exit(main())
