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
import os
import sys
import traceback

from . import jobs
from .client import BrokerNotRunning, Client, _abs
from .protocol import DEFAULT_PLAYBACK_MS, ChunkKind, JobState, Priority

from .. import __version__

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "ciqsim", "version": __version__}


def _project(args: dict) -> str:
    """Validate and normalise the `project` argument shared by every tool."""
    raw = args.get("project") or "."
    d = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isdir(d):
        raise ValueError(f"project directory does not exist: {d}")
    if not os.path.isfile(os.path.join(d, "manifest.xml")):
        raise ValueError(
            f"{d} is not a Connect IQ project (no manifest.xml)")
    return d


def _device(args: dict, proj: str) -> str:
    """The requested device, else ``[sim] device`` from ciqkit.toml, else the project's default (like the CLI).

    Resolved here, at the edge, rather than in ``Job``: protocol.py stays a
    stdlib-only wire vocabulary with no knowledge of manifests or the SDK.
    """
    from ..build import resolve_device
    return resolve_device(proj, args.get("device"))


def _name_of(project_dir: str) -> str:
    return os.path.basename(os.path.normpath(project_dir)) or "project"

DEVICE_DOC = ("simulator device id. Default: the first manifest.xml product "
              "installed in the SDK, else venu3.")

_PRIO = {"urgent": Priority.URGENT, "high": Priority.HIGH,
         "normal": Priority.NORMAL, "batch": Priority.BATCH}


def _prio(args: dict) -> int:
    return (_PRIO["urgent"] if args.get("urgent")
            else _PRIO.get(args.get("priority", "normal"), Priority.NORMAL))


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
                "name": {"type": "string", "description": "output screenshot name"},
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
                "priority": {"type": "string", "enum": list(_PRIO)},
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
                "priority": {"type": "string", "enum": list(_PRIO)},
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
                "name": {"type": "string", "description": "output clip name"},
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
                "priority": {"type": "string", "enum": list(_PRIO)},
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
                "priority": {"type": "string", "enum": list(_PRIO)},
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
                           "priority": {"type": "string", "enum": list(_PRIO), "default": "urgent"}},
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
                    _PRIO.get(args.get("priority", "urgent"), Priority.URGENT)))
            return json.dumps(c.cancel(args["job_id"]))
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
        r = c.submit_wait(job)
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
        r = c.submit_wait(job)
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
        r = c.submit_wait(job)
        return json.dumps(_nav_summary(r), indent=2)

    if name == "ciqsim_test":
        proj = _project(args)
        device = _device(args, proj)
        prg = args.get("prg")
        job = jobs.make_job(app=_name_of(proj), device=device, priority=Priority.BATCH,
                            label=f"test {_name_of(proj)}",
                            chunks=jobs.test_chunks(project=proj, device=device, prg=prg))
        r = c.submit_wait(job)
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
            st = _abs(proj, os.path.expanduser(st))
        chunks = jobs.screen_chunks(
            project=proj, device=device, name=sc.name, prg=args.get("prg"),
            anchor=sc.anchor or None, seed=sc.seed, steps=sc.steps, storage=st,
            out_dir=args.get("out_dir"), frame=not args.get("native"), force=True)
        job = jobs.make_job(app=app, device=device, priority=prio,
                            label=f"shot {sc.name}", chunks=chunks)
        try:
            r = c.submit_wait(job)
        except (RuntimeError, OSError) as exc:
            r = {"ok": False, "error": str(exc)}
        rows.append(screen_summary(sc.name, r))
    return {"ok": all(r["ok"] for r in rows), "screens": rows}


def _result_summary(r: dict) -> dict:
    job = r.get("job", {})
    # The job's own error, else the broker's answer to the WAIT itself. Reading
    # only the former turned a wait timeout into {"ok": false, "error": null}:
    # an agent read that as a failed job and resubmitted one still running.
    out = {"ok": r.get("ok"), "job_id": job.get("id"),
           "error": job.get("error") or r.get("error")}
    if r.get("error") == "wait timeout" and job.get("state") not in (
            JobState.DONE, JobState.FAILED, JobState.CANCELLED):
        out["still_running"] = True
        out["state"] = job.get("state")
        out["note"] = ("the job is still running and will still write its "
                       "output; do not resubmit it. Check it with "
                       "ciqsim_status, or cancel it with ciqsim_cancel.")
    for res in job.get("results", []):
        out.setdefault("steps", []).append({res.get("kind"): res.get("result")})
        # A finished job is not a passed one: a test run with no verdict, or
        # a nav that never reached its screen, is a DONE job whose step says
        # ok=false. The top-level ok is what an agent reads first.
        step = res.get("result")
        if isinstance(step, dict) and step.get("ok") is False:
            out["ok"] = False
    return out


def _nav_summary(r: dict) -> dict:
    """Like :func:`_result_summary`, but ``ok`` is the reach verdict.

    The job completing only means the chunks ran; nav exists to answer "did I
    reach that screen" (mirrors the CLI's ``_nav_exit``).
    """
    out = _result_summary(r)
    for step in r.get("job", {}).get("results", []):
        if step.get("kind") != ChunkKind.REACH:
            continue
        res = step.get("result") or {}
        if res.get("anchor") is False:
            out["ok"] = False
            out["error"] = (f"ran the steps but the target screen "
                            f"{res.get('screen')!r} did not show the expected text")
        elif not res.get("ok"):
            out["ok"] = False
            out["error"] = res.get("error", "nav did not reach the screen")
    return out


# ── JSON-RPC stdio loop ───────────────────────────────────────────────────────
def _respond(rid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _handle(msg: dict):
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
        try:
            text = _call_tool(name, args)
            _respond(rid, {"content": [{"type": "text", "text": text}]})
        except Exception as e:  # noqa: BLE001
            _respond(rid, {"content": [{"type": "text",
                                        "text": f"ERROR: {e}\n{traceback.format_exc()}"}],
                           "isError": True})
    elif method == "ping":
        _respond(rid, {})
    elif rid is not None:
        _respond(rid, error={"code": -32601, "message": f"method not found: {method}"})


def main():
    from .._compat import utf8_stdio
    utf8_stdio()
    for line in sys.stdin:
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
            _handle(msg)
        except Exception:
            if isinstance(msg, dict) and msg.get("id") is not None:
                _respond(msg["id"], error={"code": -32603, "message": "internal error"})


if __name__ == "__main__":
    main()
