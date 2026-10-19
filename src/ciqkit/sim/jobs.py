"""Job builders -- the one place broker jobs are assembled.

The CLI (``client.py``) and the MCP server (``mcp_server.py``) used to build
their own chunk lists, and the numbers drifted: the same screen captured through
``ciqsim_shot`` and ``ciqkit sim shot`` got different settle times and budgets.
Both now call these keyword-only builders, which take plain values -- no argparse
namespaces -- so there is exactly one definition of what a "shot", "record",
"nav" or "test" job is.

Stdlib-only, importing nothing but :mod:`.protocol`.
"""
from __future__ import annotations

import os

from .protocol import (DEFAULT_SHOT_SETTLE_S, Chunk, ChunkKind, Job, Priority,
                       validate_job)


class JobError(ValueError):
    """A job the front-end refuses to submit (before any socket call)."""


# ── Budgets (seconds) ────────────────────────────────────────────────────────
LOAD_EXPECTED_S = 40
LOAD_MAX_S = 300
SHOT_EXPECTED_S = 15.0
SHOT_MAX_S = 120.0
# nav runs every gesture in ONE chunk (it is stateful -- see SimSession.reach),
# and each verified gesture costs a delay plus up to two retries. A fixed 120 s
# meant a ten-step recipe timed out mid-nav and took a simulator restart with it.
REACH_PER_STEP_S = 8.0
REACH_MIN_MAX_S = 60.0
RESTORE_EXPECTED_S = 1
RESTORE_MAX_S = 30
RECORD_MIN_MAX_S = 60.0
TEST_EXPECTED_S = 120
TEST_MAX_PRG_S = 600
TEST_MAX_BUILD_S = 900


def abs_path(project: str, path: str | None) -> str | None:
    """``path`` made absolute against the project dir (not the caller's cwd).

    normpath: on Windows `--prg bin/App.prg` would otherwise reach monkeydo as
    `C:\\proj\\bin/App.prg`, and the storage journal keys on that string."""
    if not path:
        return path
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(project, path))


def refuse_seed_with_prg(seed, prg, *, seed_name="seed", prg_name="prg") -> None:
    """--seed patches source so the *build* picks it up; --prg skips the build.

    Together they patch the tree, push the stale pre-built binary, restore the
    tree, and hand back a fully verified screenshot of the UNSEEDED app. A
    confident wrong answer is the one failure mode this tool exists to prevent,
    so refuse rather than warn. The names are parameters so each front-end can
    say what its own user typed.
    """
    if seed and prg:
        raise JobError(
            f"{seed_name} and {prg_name} cannot be combined. {seed_name} patches "
            f"your source so the build picks it up, but {prg_name} uses an "
            f"already-built .prg and skips the build, so the capture would show "
            f"the UNSEEDED app. Drop {prg_name} to build with the seed applied, "
            f"or drop {seed_name}.")


def load_chunk(*, device, project, prg=None, force=False, storage=None,
               anchor=None, fit=None) -> Chunk:
    args = {"device": device, "project": project}
    if fit:
        # FIT playback of this file starts once the load is verified
        args["fit"] = os.path.abspath(os.path.expanduser(fit))
    # Cold reload: pushing a pre-built .prg while the same app is "warm" would
    # otherwise skip the push and re-shoot the PREVIOUS build (the stale-warm
    # bug). Callers who just rebuilt pass force.
    if force:
        args["force"] = True
    if storage:
        args["storage"] = storage if storage in ("fresh", "keep") else os.path.abspath(storage)
    if prg:
        args["prg"] = abs_path(project, prg)
    else:
        args["app_dir"] = project
    if anchor:
        args["anchor"] = anchor
    return Chunk(ChunkKind.LOAD, args, expected_s=LOAD_EXPECTED_S, max_s=LOAD_MAX_S)


def _seed_chunk(project, seed) -> Chunk:
    return Chunk(ChunkKind.SEED, {"app_dir": project, "consts": dict(seed)})


def _restore_chunk() -> Chunk:
    return Chunk(ChunkKind.RESTORE, {}, expected_s=RESTORE_EXPECTED_S, max_s=RESTORE_MAX_S)


def _num(v, default: float) -> float:
    """``v`` as a float, else ``default`` (validation reports the bad value)."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _reach_budget(n_steps: int, extra_s: float = 0.0) -> tuple[float, float]:
    return (10.0 + REACH_PER_STEP_S * n_steps + extra_s,
            max(REACH_MIN_MAX_S, 20.0 + REACH_PER_STEP_S * n_steps * 3) + extra_s)


def reach_chunk(*, screen, steps, anchor=None) -> Chunk:
    # a shot_on step waits up to its own timeout (plus the capture) for a marker
    extra = sum(_num(st.get("timeout"), MARKER_TIMEOUT_S) + SHOT_EXPECTED_S
                for st in steps if isinstance(st, dict) and st.get("kind") == "shot_on")
    expected, max_s = _reach_budget(len(steps), extra)
    return Chunk(ChunkKind.REACH,
                 {"screen": screen, "steps": steps, "anchor": anchor or None},
                 expected_s=expected, max_s=max_s)


def screen_chunks(*, project, device, name, prg=None, anchor=None, seed=None,
                  steps=None, storage=None, out_dir=None, crop=True, frame=True,
                  force=False, fit=None, marker=None, marker_timeout_s=None) -> list:
    """The chunks of one screenshot: [SEED] LOAD [REACH] SHOT [RESTORE].

    ``sim shot``, ``ciqsim_shot`` and every ``[[screens]]`` entry go through
    here, so a saved screen is the same job as the CLI line it replaces. With
    ``steps`` the anchor describes the screen the gestures end on, not the one
    the load lands on, so it moves from LOAD to REACH (LOAD would otherwise fail
    verifying text that only exists after the steps).

    ``fit`` starts FIT playback of that file right after the verified load.
    ``marker`` takes the shot when the app's console prints it (see
    :func:`marker_shot_chunks` for several); it forces a cold load, since the
    console is the one a push starts.
    """
    refuse_seed_with_prg(seed, prg)
    chunks = []
    if seed:
        chunks.append(_seed_chunk(project, seed))
    # The anchor describes the screen we expect, so it is also the strongest
    # available LOAD signal: matching it proves the right screen is up, not
    # merely that something changed.
    chunks.append(load_chunk(device=device, project=project, prg=prg,
                             force=force or bool(marker), storage=storage,
                             anchor=None if steps else anchor, fit=fit))
    if steps:
        chunks.append(reach_chunk(screen=name, steps=steps, anchor=anchor))
    chunks.append(_shot_chunk(name, crop=crop, frame=frame, anchor=anchor,
                              out_dir=out_dir, marker=marker,
                              marker_timeout_s=marker_timeout_s))
    if seed:
        chunks.append(_restore_chunk())
    return chunks


MARKER_TIMEOUT_S = 20.0


def _shot_chunk(name, *, crop=True, frame=True, anchor=None, out_dir=None,
                marker=None, marker_timeout_s=None) -> Chunk:
    args = {"name": name, "crop": crop, "frame": frame, "anchor": anchor,
            "out_dir": out_dir, "settle_s": DEFAULT_SHOT_SETTLE_S}
    wait = 0.0
    if marker:
        wait = float(marker_timeout_s or MARKER_TIMEOUT_S)
        args["marker"] = marker
        args["marker_timeout_s"] = wait
    return Chunk(ChunkKind.SHOT, args, expected_s=SHOT_EXPECTED_S + wait,
                 max_s=SHOT_MAX_S + wait)


def marker_shot_chunks(*, project, device, shots, prg=None, storage=None,
                       out_dir=None, crop=True, frame=True, fit=None,
                       timeout_s=None) -> list:
    """LOAD, then one SHOT per ``shots`` entry ``{marker, name[, timeout_s]}``.

    Each SHOT waits for its marker on the app's console and shoots; the console
    cursor only moves forward, so markers are taken in the order the app prints
    them and one printed while an earlier shot was being taken is not lost.
    The load is always cold: only a push starts a console to read.
    """
    chunks = [load_chunk(device=device, project=project, prg=prg, force=True,
                         storage=storage, fit=fit)]
    for sh in shots:
        chunks.append(_shot_chunk(sh["name"], crop=crop, frame=frame,
                                  out_dir=out_dir, marker=sh["marker"],
                                  marker_timeout_s=sh.get("timeout_s") or timeout_s))
    return chunks


def record_budget(duration_s: float) -> float:
    """The RECORD chunk's hard budget."""
    return max(RECORD_MIN_MAX_S, duration_s + 90.0)


def record_chunks(*, project, device, name, duration_s, interval_ms, playback_ms,
                  prg=None, seed=None, storage=None, out_dir=None, crop=True,
                  frame=True, keep_frames=True, force=False, fit=None) -> list:
    """[SEED] LOAD RECORD [RESTORE]."""
    refuse_seed_with_prg(seed, prg)
    chunks = []
    if seed:
        chunks.append(_seed_chunk(project, seed))
    chunks.append(load_chunk(device=device, project=project, prg=prg, force=force,
                             storage=storage, fit=fit))
    chunks.append(Chunk(ChunkKind.RECORD,
                        {"name": name, "duration_s": duration_s,
                         "interval_ms": interval_ms, "crop": crop, "frame": frame,
                         "playback_ms": playback_ms, "out_dir": out_dir,
                         "keep_frames": keep_frames,
                         "settle_s": DEFAULT_SHOT_SETTLE_S},
                        expected_s=duration_s + 8, max_s=record_budget(duration_s)))
    if seed:
        chunks.append(_restore_chunk())
    return chunks


def nav_chunks(*, project, device, to, steps, prg=None, anchor=None, force=False,
               storage=None, fit=None) -> list:
    """LOAD REACH."""
    return [load_chunk(device=device, project=project, prg=prg, force=force,
                       storage=storage, fit=fit),
            reach_chunk(screen=to, steps=steps, anchor=anchor)]


def op_chunks(steps) -> list:
    """ENSURE REACH for simulator-level ops (``phone``, ``close_aux``) that need
    the simulator up but no particular app loaded."""
    return [Chunk(ChunkKind.ENSURE, {}, expected_s=5.0, max_s=60.0),
            reach_chunk(screen="op", steps=steps)]


def test_chunks(*, project, device, prg=None, max_s=None) -> list:
    """A single TEST_BATCH chunk.

    Without ``prg`` the broker builds the ``monkeyc -t`` variant inside the job
    (the same place LOAD builds), so the build is serialised with the simulator
    and shares its timeout.
    """
    targs = {"device": device}
    if prg:
        targs["prg"] = abs_path(project, prg)
    else:
        targs["app_dir"] = project
    return [Chunk(ChunkKind.TEST_BATCH, targs, expected_s=TEST_EXPECTED_S,
                  max_s=max_s or (TEST_MAX_PRG_S if prg else TEST_MAX_BUILD_S))]


# How long a front-end waits beyond the job's own worst case: time queued behind
# or preempted by other jobs, and a rewind that replays LOAD and its gestures.
QUEUE_SLACK_S = 300.0


def wait_budget(job: Job) -> float:
    """How long a front-end should wait for ``job`` before reporting a timeout.

    Derived from the job's own chunk budgets, never picked per front-end: the
    hand-set numbers (360 s for ``sim shot``, 400 s for ``ciqsim_shot``) sat
    BELOW the broker's own ceiling for that job (LOAD 300 + SHOT 120, more with
    a seed or steps), so a slow but healthy build came back as a timeout while
    the job kept running and holding the simulator.
    """
    return sum(c.max_s for c in job.chunks) + QUEUE_SLACK_S


def make_job(*, app, device, priority=Priority.NORMAL, label="", chunks,
             owner="") -> Job:
    """Build a Job and refuse it locally if the broker would.

    Runs :func:`protocol.validate_job` here so a malformed job fails before any
    socket round trip or daemon autospawn, with every reason at once.
    """
    job = Job(app=app, chunks=list(chunks), priority=int(priority), device=device,
              owner=owner or "", label=label)
    errs = validate_job(job)
    if errs:
        raise JobError("; ".join(errs))
    return job
