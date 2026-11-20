"""MCP server exposing the simulator broker as tools.

Dependency-free: hand-rolled JSON-RPC 2.0 over stdio (newline-delimited), so
this stays pure Python with no MCP SDK or npm dependency. Any MCP client can
drive the single Connect IQ Simulator with priority, preemption and verified
capture.

Register it in your MCP client config::

  "ciqsim": {"command": "ciqkit-mcp"}

or, without the console script on PATH::

  "ciqsim": {"command": "python3", "args": ["-m", "ciqkit.sim.mcp_server"]}

TRUST MODEL -- read before enabling. Every tool takes a `project` directory,
and when no prebuilt `prg` is given the broker runs that project's build script
(`./scripts/build.sh` by default). An MCP client that can call these tools can
therefore execute a build script from any directory it names. That is the same
authority as running the CLI yourself; do not expose this server to an
untrusted client, and do not point it at projects you have not reviewed.

Tools:
  ciqsim_status                    queue, who is ahead, what is loaded
  ciqsim_shot   {project,name,...} capture one screen (build, load-verify, crop)
  ciqsim_shots  {project,all|screens} capture the [[screens]] named in ciqkit.toml
  ciqsim_record {project,name,...} record a clip to an animated GIF
  ciqsim_nav    {to,steps,...}     run gestures, verify the target screen
  ciqsim_test   {project,prg?}     run a -t build, structured pass/fail (BATCH)
  ciqsim_doctor {project?}         environment (+ project) checks, structured
  ciqsim_bump   {job_id}           raise a job's priority (jump the queue)
  ciqsim_cancel {job_id}
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time

from . import jobs
from .client import BrokerError, BrokerNotRunning, Client
from .protocol import DEFAULT_PLAYBACK_MS, Priority

from .. import __version__

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "ciqsim", "version": __version__}


log = logging.getLogger(__name__)


def _project(args: dict) -> str:
    """Validate and normalise the `project` argument shared by every tool."""
    return jobs.project_dir(args.get("project"))


def _device(args: dict, proj: str) -> str:
    """The requested device, else ``[sim] device`` from ciqkit.toml, else the project's default (like the CLI).

    Resolved here, at the edge, rather than in ``Job``: protocol.py stays a
    stdlib-only wire vocabulary with no knowledge of manifests or the SDK.
    """
    from ..build import resolve_device
    return resolve_device(proj, args.get("device"))


def _name_of(project_dir: str) -> str:
    return jobs.app_name(project_dir)

DEVICE_DOC = ("simulator device id (letters, digits, _ and -; max 64). Default: the "
              "first manifest.xml product "
              "installed in the SDK, else venu3.")

def _prio(args: dict) -> int:
    return jobs.priority(args.get("priority"), bool(args.get("urgent")))


# ── tool schemas ──────────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "ciqsim_status",
        "description": "Show the CIQ simulator broker queue: jobs, their state + "
                       "priority + ETA, which job is running, and what app/device "
                       "is loaded. Use to see who's ahead of you.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "ciqsim_shot",
        "description": "Capture ONE screenshot of a Garmin watch app through the "
                       "broker: build (if no prg) -> load + verify it actually "
                       "loaded (not the watchface) -> frame the face on the watch "
                       "body+band (store-ready) -> save the PNG. "
                       "Returns the PNG path. Framed-with-band is the DEFAULT (what "
                       "store listing images should look like); pass native:true "
                       "for the bare cropped face without the strap.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "Connect IQ project directory "
                                           "(contains manifest.xml). Default: cwd."},
                "name": {"type": "string", "pattern": "^[^/\\\\:]+$", "maxLength": 128,
                         "description": "output screenshot name: a plain file name (no path separators)"},
                "prg": {"type": "string",
                        "description": "already-built .prg, relative to the project "
                                       "dir. Omit to run the project's build script."},
                "device": {"type": "string",
                           "description": DEVICE_DOC},
                "anchor": {"type": "array", "items": {"type": "string"},
                           "description": "OCR substrings expected on the screen"},
                "out_dir": {"type": "string",
                            "description": "where to write the PNG. Default: a "
                                           "private ciqkit directory outside "
                                           "your project."},
                "fit": {"type": "string",
                        "description": "a .fit/.gpx file: start the simulator's "
                                       "FIT playback of it after the verified "
                                       "load (macOS only)"},
                "on_marker": {"type": "string",
                              "description": "take the shot when the app's "
                                             "console prints this text "
                                             "(System.println) instead of right "
                                             "after the load; always a cold load"},
                "marker_timeout_s": {"type": "number", "default": 20,
                                     "description": "how long to wait for on_marker"},
                "native": {"type": "boolean", "default": False,
                           "description": "raw cropped face WITHOUT the watch "
                                          "body+band frame. Default false = framed, "
                                          "store-ready render with the strap."},
                "force": {"type": "boolean", "default": False,
                          "description": "COLD reload: always re-push the .prg even "
                                         "if the same app is 'warm'. Set this "
                                         "whenever you just rebuilt the .prg — the "
                                         "warm-cache would otherwise re-shoot the "
                                         "PREVIOUS build's screen (the stale-warm bug)."},
                "priority": {"type": "string", "enum": list(jobs.PRIORITIES)},
                "urgent": {"type": "boolean", "description": "jump the queue (preempt a running batch)"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "ciqsim_shots",
        "description": "Capture the named screens defined in the project's "
                       "ciqkit.toml ([[screens]]: steps, anchor, seed, storage), "
                       "one framed PNG per screen at <out_dir>/<name>.png. Each "
                       "screen is its own broker job, cold-loaded, run in order. "
                       "Returns {ok, screens: [{screen, ok, path, error?}]}; ok "
                       "is false if any screen failed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "Connect IQ project directory "
                                           "(contains manifest.xml). Default: cwd."},
                "screens": {"type": "array", "items": {"type": "string"},
                            "description": "screen names to capture. Omit (or "
                                           "pass all:true) for every screen."},
                "all": {"type": "boolean", "default": False},
                "prg": {"type": "string",
                        "description": "already-built .prg, relative to the "
                                       "project dir. Incompatible with screens "
                                       "that define a seed."},
                "device": {"type": "string", "description": DEVICE_DOC},
                "storage": {"type": "string",
                            "description": "fresh|keep|DIR for every screen; "
                                           "overrides each screen's own storage"},
                "out_dir": {"type": "string",
                            "description": "where to write the PNGs. Default: a "
                                           "private ciqkit directory."},
                "native": {"type": "boolean", "default": False},
                "priority": {"type": "string", "enum": list(jobs.PRIORITIES)},
                "urgent": {"type": "boolean"},
            },
        },
    },
    {
        "name": "ciqsim_record",
        "description": "Record an animation clip of a Garmin watch app through "
                       "the broker: build (if no prg) -> load-verify -> capture "
                       "frames for duration_s -> animated GIF (real capture "
                       "timing, time-compressed so it actually animates) + a "
                       "numbered contact-sheet PNG + raw frames. Every frame is "
                       "framed like ciqsim_shot (the COMPLETE watch with "
                       "body+band on the store background), with the framing "
                       "geometry detected once so the clip never jitters; pass "
                       "native:true for the bare face crop. An optional seed patches constants in "
                       "source/Constants.mc for the build and restores them after.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "Connect IQ project directory. Default: cwd."},
                "name": {"type": "string", "pattern": "^[^/\\\\:]+$", "maxLength": 128,
                         "description": "output clip name: a plain file name (no path separators)"},
                "prg": {"type": "string",
                        "description": "already-built .prg, relative to the project "
                                       "dir. Omit to run the project's build script."},
                "device": {"type": "string",
                           "description": DEVICE_DOC},
                "out_dir": {"type": "string",
                            "description": "where to write the GIF. Default: a "
                                           "private ciqkit directory."},
                "duration_s": {"type": "number", "default": 8.0},
                "interval_ms": {"type": "integer", "default": 200},
                "playback_ms": {"type": "integer",
                                "default": DEFAULT_PLAYBACK_MS,
                                "description": "slowest median frame duration "
                                               "in the written GIF, in ms. A "
                                               "CEILING, not a target: the "
                                               "timing track is only ever sped "
                                               "UP to it, so a clip captured at "
                                               "the usual ~4.6 fps passes through "
                                               "at its real rate and only a "
                                               "slow capture gets compressed to "
                                               f"{DEFAULT_PLAYBACK_MS} (~2 fps) "
                                               "so each sampled pose stays "
                                               "readable. Real frame-gap RATIOS "
                                               "are preserved either way. 0 = "
                                               "raw wall-clock timing."},
                "native": {"type": "boolean", "default": False,
                           "description": "frames as the raw cropped face "
                                          "WITHOUT the watch body+band frame. "
                                          "Default false = framed like "
                                          "ciqsim_shot."},
                "force": {"type": "boolean", "default": False,
                          "description": "COLD reload: always re-push the .prg even "
                                         "if the same app is 'warm'. Set this "
                                         "whenever you just rebuilt the .prg — the "
                                         "warm-cache would otherwise record the "
                                         "PREVIOUS build's screen (the stale-warm bug)."},
                "seed": {"type": "object",
                         "description": "Constants.mc const overrides for the build, restored after",
                         "additionalProperties": {"type": "string"}},
                "priority": {"type": "string", "enum": list(jobs.PRIORITIES)},
                "urgent": {"type": "boolean"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "ciqsim_test",
        "description": "Run a CIQ unit-test build (monkeydo -t) through the broker "
                       "at BATCH priority and return a structured pass/fail report. "
                       "Preemptible by an urgent screenshot.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "Connect IQ project directory. Default: cwd."},
                "prg": {"type": "string",
                        "description": "a .prg built with monkeyc -t, relative to "
                                       "the project dir. Omit to have the broker "
                                       "build the unit-test variant (built-in "
                                       "monkeyc -t build; a project with a custom "
                                       "build command must pass prg)."},
                "device": {"type": "string",
                           "description": DEVICE_DOC},
            },
        },
    },
    {
        "name": "ciqsim_nav",
        "description": "Run a list of gestures on a loaded Garmin watch app "
                       "(build if no prg -> load-verify -> gestures) and verify "
                       "the target screen was reached. ok is false when a "
                       "gesture did not land or the expected anchor text was "
                       "contradicted, even though the job itself completed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "Connect IQ project directory. Default: cwd."},
                "to": {"type": "string",
                       "description": "label for the screen you expect to reach"},
                "steps": {"type": "array", "items": {"type": "object"},
                          "description": "gestures, e.g. "
                                         '[{"kind":"tap","x":0.5,"y":0.8}] '
                                         "(coordinates are display fractions); kinds: "
                                         "tap, hold, swipe (direction), key (key), "
                                         "button (id: a physical key from the "
                                         "device's simulator.json -- enter, esc, up, "
                                         "down, menu, ...; optional hold). On macOS "
                                         "use button or key enter/esc for START/BACK. "
                                         "swipe also takes from:[x,y] (display "
                                         "fractions) + dist (fraction of display "
                                         "width) for an explicit start/length, or "
                                         "edge:left|right|top|bottom for an edge "
                                         "swipe inward. fit (path): start FIT "
                                         "playback. shot_on (marker, name, "
                                         "timeout): shoot when the app's console "
                                         "prints marker"},
                "anchor": {"type": "array", "items": {"type": "string"},
                           "description": "OCR substrings expected on the target screen"},
                "prg": {"type": "string",
                        "description": "already-built .prg, relative to the project "
                                       "dir. Omit to run the project's build."},
                "device": {"type": "string",
                           "description": DEVICE_DOC},
                "force": {"type": "boolean", "default": False,
                          "description": "COLD reload: re-push the .prg even if "
                                         "the same app is warm."},
                "fit": {"type": "string",
                        "description": "a .fit/.gpx file: after the verified "
                                       "load, start the simulator's FIT "
                                       "playback of it (verified playing). "
                                       "macOS only."},
                "storage": {"type": "string",
                            "description": "fresh (default), keep, or a fixture dir"},
                "priority": {"type": "string", "enum": list(jobs.PRIORITIES)},
                "urgent": {"type": "boolean"},
            },
            "required": ["to"],
        },
    },
    {
        "name": "ciqsim_doctor",
        "description": "Check that the environment can drive the simulator "
                       "(SDK, accessibility preflight, console scripts, optional "
                       "deps) and, with project, that the project is buildable: "
                       "manifest, installed devices, developer key, build route, "
                       "monkey.jungle, tests. Returns {ok, checks:[{name, "
                       "status ok|warn|fail|info, detail, notes}]}. Does not "
                       "start the broker.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "Connect IQ project directory to "
                                           "check as well. Omit for the "
                                           "environment checks only."},
            },
        },
    },
    {
        "name": "ciqsim_bump",
        "description": "Raise a queued/running job's priority so it runs next "
                       "(e.g. push a screenshot ahead of a long regression run).",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"},
                           "priority": {"type": "string", "enum": list(jobs.PRIORITIES), "default": "urgent"}},
            "required": ["job_id"],
        },
    },
    {
        "name": "ciqsim_cancel",
        "description": "Cancel a job by id.",
        "inputSchema": {"type": "object",
                        "properties": {"job_id": {"type": "string"}},
                        "required": ["job_id"]},
    },
]


# ── tool dispatch ─────────────────────────────────────────────────────────────
def _call_tool(name: str, args: dict) -> str:
    # Looking at the queue must not create one. With autospawn these three would
    # boot a broker that then idles for ten minutes, so an agent that merely
    # checked status has silently started a daemon.
    if name in ("ciqsim_status", "ciqsim_bump", "ciqsim_cancel"):
        c = Client(autospawn=False)
        try:
            if name == "ciqsim_status":
                return json.dumps(c.status(), indent=2)
            if name == "ciqsim_bump":
                return json.dumps(c.bump(
                    args["job_id"],
                    jobs.priority(args.get("priority") or "urgent")))
            return json.dumps(c.cancel(args["job_id"]))
        except BrokerError as e:
            # The broker's answer, not a tool failure: `no such job` is routine
            # once retention has reaped a job. The agent reads the reply itself,
            # `code` included.
            return json.dumps(e.reply, indent=2 if name == "ciqsim_status" else None)
        except BrokerNotRunning:
            # A fact, not an error: there is no queue because nothing is
            # running. Any other socket failure is a real error and is left to
            # propagate -- telling an agent "nothing is running" when the
            # broker died mid-reply sends it to resubmit work that may still
            # be in flight.
            return json.dumps({"ok": False, "running": False,
                               "note": "no broker is running; it starts "
                                       "automatically when a shot, record or "
                                       "test job is submitted"}, indent=2)

    if name == "ciqsim_doctor":
        from .. import doctor
        proj = None
        if args.get("project"):
            proj = os.path.abspath(os.path.expanduser(args["project"]))
        return json.dumps(doctor.run(proj), indent=2)

    c = Client()

    if name == "ciqsim_shot":
        proj = _project(args)
        device = _device(args, proj)
        # force: cold reload -- pushing a pre-built .prg while the same app is
        # "warm" would otherwise re-shoot the PREVIOUS build (the stale-warm bug).
        chunks = jobs.screen_chunks(
            project=proj, device=device, name=args["name"], prg=args.get("prg"),
            anchor=args.get("anchor"), out_dir=args.get("out_dir"),
            frame=not args.get("native", False), force=bool(args.get("force")),
            fit=args.get("fit"), marker=args.get("on_marker"),
            marker_timeout_s=args.get("marker_timeout_s"))
        job = jobs.make_job(app=_name_of(proj), device=device, priority=_prio(args),
                            label=f"shot {args['name']}", chunks=chunks)
        r = _submit_wait(c, job)
        return json.dumps(_result_summary(r), indent=2)

    if name == "ciqsim_shots":
        return json.dumps(_shots_tool(c, args), indent=2)

    if name == "ciqsim_record":
        proj = _project(args)
        device = _device(args, proj)
        dur = float(args.get("duration_s", 8.0))
        chunks = jobs.record_chunks(
            project=proj, device=device, name=args["name"], duration_s=dur,
            interval_ms=int(args.get("interval_ms", 200)),
            playback_ms=int(args.get("playback_ms", DEFAULT_PLAYBACK_MS)),
            prg=args.get("prg"), seed=args.get("seed") or None,
            out_dir=args.get("out_dir"), frame=not args.get("native", False),
            force=bool(args.get("force")))
        job = jobs.make_job(app=_name_of(proj), device=device, priority=_prio(args),
                            label=f"record {args['name']}", chunks=chunks)
        r = _submit_wait(c, job)
        return json.dumps(_result_summary(r), indent=2)

    if name == "ciqsim_nav":
        proj = _project(args)
        if not args.get("to"):
            raise ValueError("`to` (the target screen label) is required")
        steps = args.get("steps") or []
        if not isinstance(steps, list):
            raise ValueError("`steps` must be a list of gesture objects")
        device = _device(args, proj)
        chunks = jobs.nav_chunks(
            project=proj, device=device, to=args["to"], steps=steps,
            prg=args.get("prg"), anchor=args.get("anchor"),
            force=bool(args.get("force")), storage=args.get("storage"),
            fit=args.get("fit"))
        job = jobs.make_job(app=_name_of(proj), device=device, priority=_prio(args),
                            label=f"nav {args['to']}", chunks=chunks)
        r = _submit_wait(c, job)
        return json.dumps(_nav_summary(r), indent=2)

    if name == "ciqsim_test":
        proj = _project(args)
        device = _device(args, proj)
        prg = args.get("prg")
        job = jobs.make_job(app=_name_of(proj), device=device, priority=Priority.BATCH,
                            label=f"test {_name_of(proj)}",
                            chunks=jobs.test_chunks(project=proj, device=device, prg=prg))
        r = _submit_wait(c, job)
        return json.dumps(_result_summary(r), indent=2)

    raise ValueError(f"unknown tool {name}")


def _shots_tool(c, args: dict) -> dict:
    """``ciqsim_shots``: the same per-screen jobs as ``ciqkit sim shots``."""
    from ..config import ConfigError, load_config
    from .client import screen_summary
    proj = _project(args)
    app = _name_of(proj)
    try:
        cfg = load_config(proj)
    except ConfigError as exc:
        raise ValueError(str(exc)) from exc
    if not cfg.screens:
        raise ValueError("no [[screens]] defined in ciqkit.toml")
    names = args.get("screens") or []
    if args.get("all") or not names:
        picked = list(cfg.screens)
    else:
        picked = []
        for n in names:
            sc = cfg.screen(n)
            if sc is None:
                raise ValueError(f"no screen {n!r}; defined: "
                                 f"{', '.join(s.name for s in cfg.screens)}")
            if sc not in picked:
                picked.append(sc)
    # Refuse before the first screen is submitted, not midway through the list.
    if any(sc.seed for sc in picked):
        jobs.refuse_seed_with_prg(True, args.get("prg"))
    device = _device(args, proj)
    prio = _prio(args)
    storage = args.get("storage")
    rows = []
    for sc in picked:
        st = storage or sc.storage
        if st and not storage and st not in ("fresh", "keep"):
            st = jobs.abs_path(proj, os.path.expanduser(st))
        chunks = jobs.screen_chunks(
            project=proj, device=device, name=sc.name, prg=args.get("prg"),
            anchor=sc.anchor or None, seed=sc.seed, steps=sc.steps, storage=st,
            out_dir=args.get("out_dir"), frame=not args.get("native"), force=True)
        job = jobs.make_job(app=app, device=device, priority=prio,
                            label=f"shot {sc.name}", chunks=chunks)
        try:
            r = _submit_wait(c, job)
        except (RuntimeError, OSError) as exc:
            r = {"ok": False, "error": str(exc)}
        rows.append(screen_summary(sc.name, r))
    return {"ok": all(r["ok"] for r in rows), "screens": rows}


def _result_summary(r: dict, command: str = jobs.JOB) -> dict:
    """The tool's answer to a finished WAIT; ``ok``/``error`` are
    :func:`jobs.verdict`'s, everything else is formatting."""
    job = r.get("job", {})
    v = jobs.verdict(r, command)
    # `error` is the job's own error, else the broker's answer to the WAIT
    # itself. Reading only the former turned a wait timeout into
    # {"ok": false, "error": null}: an agent read that as a failed job and
    # resubmitted one still running. A finished job is not a passed one
    # either: a test run with no verdict, or a nav that never reached its
    # screen, is a DONE job whose step says ok=false -- and the top-level ok
    # is what an agent reads first.
    out = {"ok": v.ok, "job_id": job.get("id"), "error": v.reason}
    if v.still_running:
        out["still_running"] = True
        out["state"] = job.get("state")
        out["note"] = ("the job is still running and will still write its "
                       "output; do not resubmit it. Check it with "
                       "ciqsim_status, or cancel it with ciqsim_cancel.")
    for res in job.get("results", []):
        out.setdefault("steps", []).append({res.get("kind"): res.get("result")})
    return out


def _nav_summary(r: dict) -> dict:
    """Like :func:`_result_summary`, but ``ok`` is the reach verdict: the job
    completing only means the chunks ran; nav answers "did I reach that screen"."""
    return _result_summary(r, jobs.NAV)


# ── in-flight tool calls ──────────────────────────────────────────────────────
# Each tools/call runs on its own worker thread, so the stdio loop keeps reading
# while a shot or test job waits minutes for the simulator: ping, tools/list and
# notifications/cancelled are answered meanwhile. The owner's rule for a
# cancelled call is that its broker job is cancelled too -- an agent that gave
# up on a shot must not leave it queued in front of everyone else.

# How long EOF waits for workers to see their cancelled job finish before the
# process exits anyway (they are daemon threads; the broker job is already
# cancelled by then, so nothing is left running on our behalf).
EOF_GRACE_S = 10.0


class _Cancelled(Exception):
    """The client cancelled this tools/call; submit nothing more for it.

    Not a RuntimeError/OSError on purpose: ``_shots_tool`` catches those per
    screen, and a cancel must end the whole list, not just one screen."""


class _Call:
    """One in-flight tools/call and the broker jobs it submitted
    (``ciqsim_shots`` submits several, one after another).

    ``stopping``: its jobs are cancelled and it must submit no more (a client
    cancel, or stdin EOF). ``cancelled``: the client cancelled it, so it gets
    no response. EOF sets only the former -- a client that merely half-closed
    stdin still reads the answers of calls that were already finishing.
    """
    __slots__ = ("cancelled", "stopping", "job_ids")

    def __init__(self):
        self.cancelled = False
        self.stopping = False
        self.job_ids: list[str] = []


_inflight: dict = {}                 # JSON-RPC request id -> _Call
_inflight_lock = threading.Lock()    # guards _inflight and every _Call's fields
_out_lock = threading.Lock()         # one writer at a time on the JSON-RPC stdout
_tls = threading.local()             # .call: the _Call the current worker serves


def _submit_wait(c, job) -> dict:
    """``Client.submit_wait``, in two steps when serving a tracked tools/call so
    the call learns its job id before the long wait and a cancel can reach it.

    Outside a worker (a direct ``_call_tool``) it is plain ``submit_wait``.
    A wait *timeout* is still not a cancellation: only a client's
    notifications/cancelled, or stdin EOF, cancels the job.
    """
    call = getattr(_tls, "call", None)
    if call is None:
        return c.submit_wait(job)
    with _inflight_lock:
        if call.stopping:
            raise _Cancelled()
    jid = c.submit(job)
    with _inflight_lock:
        call.job_ids.append(jid)
        late = call.stopping
    if late:
        # The cancel arrived while SUBMIT was on the wire, so it could not have
        # seen this id: cancel it here rather than leave it queued.
        _cancel_jobs([jid])
        raise _Cancelled()
    return c.wait(jid, jobs.wait_budget(job))


def _cancel_jobs(job_ids) -> None:
    """Best-effort broker CANCEL for each job. Never autospawns: a broker that
    is gone has nothing left to cancel."""
    if not job_ids:
        return
    try:
        c = Client(autospawn=False)
    except Exception:  # noqa: BLE001
        log.warning("cannot reach the broker to cancel %s", job_ids, exc_info=True)
        return
    for jid in job_ids:
        try:
            c.cancel(jid)
        except Exception:  # noqa: BLE001 -- one failed cancel must not skip the rest
            log.warning("could not cancel job %s", jid, exc_info=True)


def _run_call(rid, call: _Call, name, args: dict) -> None:
    """Worker body for one tools/call."""
    _tls.call = call
    result = None
    try:
        text = _call_tool(name, args)
        result = {"content": [{"type": "text", "text": text}]}
    except _Cancelled:
        pass
    except Exception as e:  # noqa: BLE001
        # The traceback goes to the log, not the model: it is noise to an
        # agent and leaks local paths. The type name stays, because str() of
        # a KeyError/TypeError from a bad argument is empty or a bare key.
        log.exception("tool %s failed", name)
        result = {"content": [{"type": "text",
                               "text": f"ERROR: {type(e).__name__}: {e}"}],
                  "isError": True}
    finally:
        _tls.call = None
        with _inflight_lock:
            if _inflight.get(rid) is call:
                del _inflight[rid]
            cancelled = call.cancelled
    # MCP: a cancelled request gets no response at all.
    if result is not None and not cancelled:
        _respond(rid, result)


def _cancel_request(rid):
    """notifications/cancelled: mark the call and cancel its broker jobs. An
    unknown or finished id is ignored, as the spec says (the race is normal)."""
    with _inflight_lock:
        call = _inflight.get(rid)
        if call is None or call.cancelled:
            return None
        already = call.stopping
        call.cancelled = call.stopping = True
        job_ids = [] if already else list(call.job_ids)
    if not job_ids:
        return None   # not submitted yet: _submit_wait sees the flag
    # Off the read loop: a CANCEL to a wedged broker must not stall pings.
    t = threading.Thread(target=_cancel_jobs, args=(job_ids,),
                         name="mcp-cancel", daemon=True)
    t.start()
    return t


def _cancel_all() -> None:
    """stdin EOF: the client is gone, so nobody is waiting for these jobs."""
    with _inflight_lock:
        job_ids = []
        for call in _inflight.values():
            if not call.stopping:    # an already-cancelled call's jobs were too
                call.stopping = True
                job_ids += call.job_ids
    _cancel_jobs(job_ids)


# ── JSON-RPC stdio loop ───────────────────────────────────────────────────────
def _respond(rid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    line = json.dumps(msg) + "\n"
    # Workers answer concurrently with the read loop; two half-written lines
    # would corrupt the newline framing for every message after them.
    with _out_lock:
        try:
            sys.stdout.write(line)
            sys.stdout.flush()
        except (OSError, ValueError):
            # The client closed its end (EOF, then a late worker answer): there
            # is no one to tell, and a traceback from a worker thread helps no one.
            log.debug("dropped response %r: stdout is closed", rid)


def _handle(msg: dict):
    """Handle one message. A tools/call is started on a worker thread, which
    is returned (so is a cancel's); everything else is answered inline."""
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialize":
        _respond(rid, {"protocolVersion": PROTOCOL_VERSION,
                       "capabilities": {"tools": {}},
                       "serverInfo": SERVER_INFO})
    elif method == "notifications/initialized":
        pass  # notification, no reply
    elif method == "tools/list":
        _respond(rid, {"tools": TOOLS})
    elif method == "tools/call":
        params = msg.get("params", {})
        name = params.get("name")
        args = params.get("arguments", {}) or {}
        call = _Call()
        with _inflight_lock:
            _inflight[rid] = call
        t = threading.Thread(target=_run_call, args=(rid, call, name, args),
                             name=f"mcp-call-{rid}", daemon=True)
        t.start()
        return t
    elif method == "notifications/cancelled":
        return _cancel_request((msg.get("params") or {}).get("requestId"))
    elif method == "ping":
        _respond(rid, {})
    elif rid is not None:
        _respond(rid, error={"code": -32601, "message": f"method not found: {method}"})
    return None


def _setup_logging() -> None:
    """Send ``log.exception`` somewhere: stderr, which MCP clients keep as the
    server's log. Never stdout -- that is the JSON-RPC channel. brokerd.log is
    the broker's, not ours."""
    lvl = logging.getLevelName(os.environ.get("CIQKIT_LOG_LEVEL", "WARNING").upper())
    logging.basicConfig(
        level=lvl if isinstance(lvl, int) else logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s [ciqkit-mcp] %(name)s: %(message)s",
    )


def main():
    from .._compat import utf8_stdio
    utf8_stdio()
    _setup_logging()
    serve(sys.stdin)


def serve(lines, grace_s: float = EOF_GRACE_S) -> None:
    """Read newline-delimited JSON-RPC from ``lines`` until EOF, then cancel
    every in-flight call's broker job and give the workers ``grace_s`` to see
    it before returning."""
    workers: list[threading.Thread] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError as exc:
            # Not framed JSON. There is no id to answer with, so the only
            # options are to drop the line or to kill the session; dropping
            # keeps a client that sends a stray keepalive alive.
            print(f"[ciqkit-mcp] ignoring malformed line: {exc}", file=sys.stderr)
            continue
        try:
            t = _handle(msg)
        except Exception:
            log.exception("unhandled error handling message")
            if isinstance(msg, dict) and msg.get("id") is not None:
                _respond(msg["id"], error={"code": -32603, "message": "internal error"})
            continue
        if t is not None:
            workers = [w for w in workers if w.is_alive()]
            workers.append(t)
    # EOF: the client is gone. Cancel what it was waiting for -- nobody will
    # read the results -- then let the workers wind down, bounded.
    _cancel_all()
    deadline = time.monotonic() + grace_s
    for w in workers:
        w.join(max(0.0, deadline - time.monotonic()))


if __name__ == "__main__":
    main()
