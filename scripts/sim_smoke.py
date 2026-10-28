#!/usr/bin/env python3
"""Real-simulator smoke run: every public session op once, each one checked.

The test suite only ever drives a FakeSession, so nothing in CI says whether a
gesture, a FIT playback or the phone menu still works on a real simulator. This
script does, on the machine it runs on, and appends one JSON line per op to
docs/sim-smoke.jsonl:

    {op, ok, detail, platform, device, sdk, sha, at}

That file is committed, so runs on the macOS and the Windows desktop both
count; scripts/sim_smoke_reading.py turns it into the simOpsVerified reading.

It goes through the broker (Client + the jobs.py builders the CLI uses), never
SimSession: one actor drives the simulator. Each op is its own job starting
from a forced push of the example, so one op's failure cannot leave the next
on the wrong screen. An op the platform cannot do is written as ok=false with a
"skipped:" reason -- a skip is never a pass.

    python3 scripts/sim_smoke.py                  # all ops, venu3
    python3 scripts/sim_smoke.py --only shot,record

Module level is stdlib-only: the reading script imports OPS from here under a
bare python3 that has no ciqkit installed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "docs", "sim-smoke.jsonl")
PROJECT = os.path.join(ROOT, "examples", "tictactoe")

# The public ops, in run order. seed_restore runs late because its seeded build
# overwrites bin/tictactoe.prg, which every op before it pushes.
OPS = ("load", "act_tap", "act_swipe", "act_swipe_edge", "act_key", "button",
       "reach", "shot", "record", "fit_playback", "close_aux", "set_phone",
       "seed_restore", "test")

# What screenshot_utils implements for macOS only.
_MACOS_ONLY = {
    "fit_playback": "FIT playback control is implemented for macOS only",
    "set_phone": "phone connection control is implemented for macOS only",
}


def platform_name() -> str:
    return {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)


def skip_reason(op: str, plat: str) -> str | None:
    if plat not in ("macos", "windows"):
        return "no simulator window control on this platform (macOS and Windows only)"
    if plat != "macos" and op in _MACOS_ONLY:
        return _MACOS_ONLY[op]
    return None


def _git_sha() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                               capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    # A run from a dirty tree did not test exactly that commit; say so.
    return sha + ("+dirty" if dirty else "")


def _sdk_version() -> str:
    from ciqkit.sim.preflight import detect_sdk
    sdk = detect_sdk()
    m = re.search(r"\d+\.\d+\.\d+", os.path.basename(sdk or ""))
    return m.group(0) if m else (sdk or "none")


# ── job plumbing ──────────────────────────────────────────────────────────────

class OpFailed(Exception):
    pass


class Smoke:
    def __init__(self, device: str):
        from ciqkit.build import prg_name
        from ciqkit.sim.client import Client
        self.c = Client()
        self.device = device
        self.app = os.path.basename(PROJECT)
        self.prg = os.path.join(PROJECT, "bin", prg_name(PROJECT) + ".prg")

    def run(self, label: str, chunks: list) -> list:
        """Submit, wait, and return the per-chunk results; raise on a failed job."""
        from ciqkit.sim import jobs
        job = jobs.make_job(app=self.app, device=self.device, chunks=chunks,
                            label=f"smoke:{label}",
                            owner=os.environ.get("CIQKIT_SESSION_LABEL", "sim-smoke"))
        r = self.c.submit_wait(job)
        j = r.get("job") or {}
        if not r.get("ok"):
            raise OpFailed(f"job {j.get('state', '?')}: "
                           f"{j.get('error') or r.get('error') or 'no reason given'}")
        return [x.get("result") or {} for x in j.get("results", [])]

    def load(self, **kw):
        from ciqkit.sim import jobs
        # force: every op starts from a fresh push of the same build, i.e. on the
        # home screen, whatever the op before it left up.
        return jobs.load_chunk(device=self.device, project=PROJECT, prg=self.prg,
                               force=True, **kw)

    @staticmethod
    def act(**args):
        from ciqkit.sim.protocol import Chunk, ChunkKind
        return Chunk(ChunkKind.ACT, args)

    @staticmethod
    def landed(res: dict, what: str) -> str:
        if not (res.get("ok") and res.get("confirmed")):
            raise OpFailed(f"{what} did not land: {res.get('reason') or res}")
        return f"{what} confirmed (attempts={res.get('attempts')}, changed={res.get('changed')})"


# ── the ops ───────────────────────────────────────────────────────────────────
# Each returns a detail string on success and raises OpFailed otherwise. The
# example's controls (examples/tictactoe/README.md): START or a tap starts a
# game from home, swipes up/down move the cursor, swipe right / BACK go home.

def op_load(s: Smoke) -> str:
    from ciqkit.sim import jobs
    # app_dir, not prg: this is the build the later ops push.
    (res,) = s.run("load", [jobs.load_chunk(device=s.device, project=PROJECT,
                                            force=True, anchor=["toe"])])
    if not res.get("ok") or res.get("warm") or not res.get("verified"):
        raise OpFailed(f"load not verified: {res}")
    if not os.path.isfile(s.prg):
        raise OpFailed(f"load passed but {s.prg} is missing")
    return (f"built and pushed, verified={res['verified']} attempts={res.get('attempts')} "
            f"anchor_confirmed={res.get('anchor_confirmed')}")


def op_act_tap(s: Smoke) -> str:
    *_, res = s.run("act_tap", [s.load(), s.act(kind="tap", x=0.5, y=0.5)])
    return s.landed(res, "tap home -> game")


def _in_game(s: Smoke, label: str, gesture) -> dict:
    load, start, res = s.run(label, [s.load(), s.act(kind="tap", x=0.5, y=0.5), gesture])
    s.landed(start, "setup tap home -> game")
    return res


def op_act_swipe(s: Smoke) -> str:
    res = _in_game(s, "act_swipe", s.act(kind="swipe", direction="up"))
    return s.landed(res, "swipe up moves the cursor")


def op_act_swipe_edge(s: Smoke) -> str:
    res = _in_game(s, "act_swipe_edge", s.act(kind="swipe", edge="left"))
    return s.landed(res, "edge swipe in from the left (rightward) game -> home")


def op_act_key(s: Smoke) -> str:
    *_, res = s.run("act_key", [s.load(), s.act(kind="key", key="enter")])
    note = " (macOS routes enter through the button click)" if platform_name() == "macos" else ""
    return s.landed(res, "key enter home -> game") + note


def op_button(s: Smoke) -> str:
    _, start, back = s.run("button", [s.load(), s.act(kind="button", id="enter"),
                                      s.act(kind="button", id="esc")])
    return s.landed(start, "button enter home -> game") + "; " + \
        s.landed(back, "button esc game -> home")


def op_reach(s: Smoke) -> str:
    from ciqkit.sim import jobs
    steps = [{"kind": "key", "key": "enter"}] * 3
    _, res = s.run("reach", [s.load(), jobs.reach_chunk(screen="mid-game", steps=steps)])
    done = res.get("steps") or []
    if not res.get("ok") or len(done) != len(steps) or not all(d.get("ok") for d in done):
        raise OpFailed(f"reach mid-game failed: {res.get('error') or res}")
    return f"reached mid-game in {len(done)} confirmed steps"


def _not_blank(path: str) -> None:
    from PIL import Image
    with Image.open(path) as im:
        lo_hi = im.convert("L").getextrema()
    if lo_hi[0] == lo_hi[1]:
        raise OpFailed(f"{path} is a single flat colour")


def op_shot(s: Smoke) -> str:
    from ciqkit.sim import jobs
    chunks = jobs.screen_chunks(project=PROJECT, device=s.device, name="smoke-home",
                                prg=s.prg, anchor=["toe"], force=True, frame=False)
    res = s.run("shot", chunks)[-1]
    path = res.get("path")
    if not res.get("ok") or not path or not os.path.isfile(path):
        raise OpFailed(f"no screenshot: {res}")
    _not_blank(path)
    if res.get("anchor") is False:
        raise OpFailed(f"OCR ran and 'toe' is not on the home screen ({path})")
    return f"home captured, not blank, anchor={res.get('anchor')}"


def op_record(s: Smoke) -> str:
    from ciqkit.sim import jobs
    from ciqkit.sim.protocol import DEFAULT_PLAYBACK_MS
    chunks = jobs.record_chunks(project=PROJECT, device=s.device, name="smoke-rec",
                                duration_s=3.0, interval_ms=200,
                                playback_ms=DEFAULT_PLAYBACK_MS, prg=s.prg, force=True,
                                keep_frames=False)
    res = s.run("record", chunks)[-1]
    gif = res.get("gif")
    if not res.get("ok") or not gif or not os.path.isfile(gif):
        raise OpFailed(f"no gif: {res}")
    if (res.get("frames") or 0) < 2:
        raise OpFailed(f"only {res.get('frames')} frame(s) in {res.get('duration_s')} s")
    return f"{res['frames']} frames in {res.get('duration_s')} s ({res.get('fps_actual')} fps)"


# A short synthetic GPX track (no Garmin material): 90 one-second points.
def _gpx(path: str) -> None:
    pts = "\n".join(
        f'<trkpt lat="{47.0 + i * 0.00005:.6f}" lon="8.000000">'
        f"<ele>{400 + i * 0.5:.1f}</ele>"
        f"<time>2026-01-01T10:{i // 60:02d}:{i % 60:02d}Z</time></trkpt>"
        for i in range(90))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                 '<gpx version="1.1" creator="ciqkit-sim-smoke" '
                 'xmlns="http://www.topografix.com/GPX/1/1">\n'
                 f"<trk><name>smoke</name><trkseg>\n{pts}\n</trkseg></trk></gpx>\n")


def op_fit_playback(s: Smoke) -> str:
    with tempfile.TemporaryDirectory(prefix="ciqkit-smoke-") as d:
        track = os.path.join(d, "smoke.gpx")
        _gpx(track)
        (res,) = s.run("fit_playback", [s.load(fit=track)])
    fit = res.get("fit") or {}
    if not res.get("ok") or not fit or not (fit.get("elapsed_s") or 0) > 0:
        raise OpFailed(f"playback not verified: {res}")
    return f"playing {os.path.basename(str(fit.get('file')))}, elapsed {fit.get('elapsed_s')} s"


def _op_steps(s: Smoke, label: str, steps: list) -> list:
    from ciqkit.sim import jobs
    _, res = s.run(label, jobs.op_chunks(steps))
    done = res.get("steps") or []
    if len(done) != len(steps):
        raise OpFailed(f"{label}: ran {len(done)} of {len(steps)} steps: {res}")
    return done


def op_close_aux(s: Smoke) -> str:
    (res,) = _op_steps(s, "close_aux", [{"kind": "close_aux"}])
    if not res.get("ok"):
        raise OpFailed(f"close_aux: {res}")
    # Nothing in the example opens a web view, so this proves the op runs and
    # finds the device window, not that it closes a real aux window.
    return f"ran; {json.dumps({k: v for k, v in res.items() if k != 'ok'})[:160]}"


def op_set_phone(s: Smoke) -> str:
    # Disconnect, then put it back: the setting persists in the simulator.
    off, on = _op_steps(s, "set_phone", [{"kind": "phone", "state": "disconnected"},
                                          {"kind": "phone", "state": "connected"}])
    # `state` is the menu's own label ("Not Connected"); phoneConnected is the
    # boolean the session derives from it.
    for want, res in ((False, off), (True, on)):
        if not res.get("ok") or res.get("phoneConnected") is not want:
            raise OpFailed(f"phone connected={want}: menu reads back {res}")
    return "disconnected then connected, both read back from the menu"


def op_seed_restore(s: Smoke) -> str:
    from ciqkit.sim import jobs
    consts = os.path.join(PROJECT, "source", "Constants.mc")
    with open(consts, "rb") as fh:
        before = fh.read()
    chunks = jobs.screen_chunks(project=PROJECT, device=s.device, name="smoke-seed",
                                seed={"AI_LEVEL": "0"}, frame=False)
    seed, *_, restore = s.run("seed_restore", chunks)
    with open(consts, "rb") as fh:
        after = fh.read()
    if not seed.get("ok"):
        raise OpFailed(f"seed: {seed}")
    if not restore.get("ok") or restore.get("git_clean") is False or after != before:
        raise OpFailed(f"restore not byte-exact: {restore}, bytes equal={after == before}")
    return f"seeded AI_LEVEL=0, built, shot, restored byte-exact (git_clean={restore.get('git_clean')})"


def op_test(s: Smoke) -> str:
    from ciqkit.sim import jobs
    (res,) = s.run("test", jobs.test_chunks(project=PROJECT, device=s.device))
    if not res.get("ok") or not res.get("ran"):
        raise OpFailed(f"tests: {res.get('reason') or res}")
    return f"ran {res['ran']}, passed {res.get('passed')}, failed {res.get('failed')}"


# ── main ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--device", default="venu3")
    ap.add_argument("--only", help="comma-separated subset of: " + ",".join(OPS))
    args = ap.parse_args(argv)
    ops = OPS
    if args.only:
        ops = tuple(o.strip() for o in args.only.split(",") if o.strip())
        bad = [o for o in ops if o not in OPS]
        if bad:
            ap.error(f"unknown op(s) {bad}; choose from {list(OPS)}")

    plat = platform_name()
    base = {"platform": plat, "device": args.device, "sha": _git_sha()}
    smoke = None
    if all(skip_reason(o, plat) for o in ops):
        base["sdk"] = "n/a"
    else:
        base["sdk"] = _sdk_version()
        smoke = Smoke(args.device)

    failed = 0
    load_failed = None
    for op in ops:
        ok, detail = False, ""
        why = skip_reason(op, plat)
        if why:
            detail = f"skipped: {why}"
        elif load_failed and op not in ("load", "close_aux", "set_phone", "seed_restore", "test"):
            # Every other op pushes the prg the load op builds; with that build
            # failed it would push a stale one and could pass on it.
            detail = f"not run: the load op failed ({load_failed})"
        else:
            try:
                detail = globals()["op_" + op](smoke)
                ok = True
            except OpFailed as exc:
                detail = str(exc)
            except Exception as exc:   # a crashed op is a finding, not a crashed run
                detail = f"{type(exc).__name__}: {exc}"
            if op == "load" and not ok:
                load_failed = detail[:120]
        failed += not ok and not why
        line = {"op": op, "ok": ok, "detail": detail[:500], **base,
                "at": datetime.now(UTC).isoformat(timespec="seconds")}
        with open(LOG, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(line) + "\n")
        print(f"{'PASS' if ok else 'SKIP' if why else 'FAIL'}  {op:15} {detail}",
              file=sys.stderr, flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
