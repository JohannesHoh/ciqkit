"""Job builders -- the one place broker jobs are assembled.

The CLI (``client.py``) and the MCP server (``mcp_server.py``) used to build
their own chunk lists, and the numbers drifted: the same screen captured through
``ciqsim_shot`` and ``ciqkit sim shot`` got different settle times and budgets.
Both now call these keyword-only builders, which take plain values -- no argparse
namespaces -- so there is exactly one definition of what a "shot", "record",
"nav" or "test" job is. :func:`verdict` is the matching single definition of
whether such a job passed.

Stdlib-only, importing nothing but :mod:`.protocol`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .protocol import (CHUNK_BUDGETS, DEFAULT_SHOT_SETTLE_S, E_WAIT_TIMEOUT, Chunk, ChunkKind,
                       Job, JobState, Priority, error_code, validate_job)


class JobError(ValueError):
    """A job the front-end refuses to submit (before any socket call)."""


# ── Budgets (seconds) ────────────────────────────────────────────────────────
# Fixed budgets live in protocol.CHUNK_BUDGETS (one copy, shared with
# Chunk.from_dict); these names stay for the builders and existing callers.
LOAD_EXPECTED_S, LOAD_MAX_S = CHUNK_BUDGETS[ChunkKind.LOAD]
SHOT_EXPECTED_S, SHOT_MAX_S = CHUNK_BUDGETS[ChunkKind.SHOT]
ENSURE_EXPECTED_S, ENSURE_MAX_S = CHUNK_BUDGETS[ChunkKind.ENSURE]
# nav runs every gesture in ONE chunk (it is stateful -- see SimSession.reach),
# and each verified gesture costs a delay plus up to two retries. A fixed 120 s
# meant a ten-step recipe timed out mid-nav and took a simulator restart with it.
REACH_PER_STEP_S = 8.0
REACH_MIN_MAX_S = 60.0
RESTORE_EXPECTED_S, RESTORE_MAX_S = CHUNK_BUDGETS[ChunkKind.RESTORE]
RECORD_MIN_MAX_S = 60.0
TEST_EXPECTED_S, TEST_MAX_BUILD_S = CHUNK_BUDGETS[ChunkKind.TEST_BATCH]
TEST_MAX_PRG_S = 600


def abs_path(project: str, path: str | None) -> str | None:
    """``path`` made absolute against the project dir (not the caller's cwd).

    normpath: on Windows `--prg bin/App.prg` would otherwise reach monkeydo as
    `C:\\proj\\bin/App.prg`, and the storage journal keys on that string."""
    if not path:
        return path
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(project, path))


def project_dir(raw: str | None, *, flag: str = "project", hint: str = "") -> str:
    """The Connect IQ project directory ``raw`` names (default: cwd), validated.

    One definition for the CLI and the MCP server: ``~`` is expanded (an MCP
    client or a quoted ``--project "~/app"`` never gets a shell to do it), and a
    wrong directory is refused here instead of surfacing later as a confusing
    build error. ``flag``/``hint`` let each front-end word the refusal for what
    its own user typed.
    """
    d = os.path.abspath(os.path.expanduser(raw or "."))
    if not os.path.isdir(d):
        raise JobError(f"{flag} directory does not exist: {d}")
    if not os.path.isfile(os.path.join(d, "manifest.xml")):
        raise JobError(f"{d} does not look like a Connect IQ project "
                       f"(no manifest.xml).{hint}")
    return d


def app_name(project_dir: str) -> str:
    """Short label for status output; also the broker's warm-app key."""
    return os.path.basename(os.path.normpath(project_dir)) or "project"


PRIORITIES = {"urgent": Priority.URGENT, "high": Priority.HIGH,
              "normal": Priority.NORMAL, "batch": Priority.BATCH}


def priority(name: str | None, urgent: bool = False) -> Priority:
    """Priority for ``name`` (default normal); ``urgent`` wins. Unknown -> JobError.

    Falling back silently turned a typo into URGENT on the MCP bump path."""
    if urgent:
        return Priority.URGENT
    key = name or "normal"
    try:
        return PRIORITIES[key]
    except KeyError:
        raise JobError(f"unknown priority {key!r} "
                       f"(expected one of: {', '.join(PRIORITIES)})") from None


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
    # A shot_on step may carry its own out_dir; like SHOT's it must reach the
    # broker absolute (validate_job refuses a relative one).
    steps = [dict(st, out_dir=abs_out_dir(st["out_dir"]))
             if isinstance(st, dict) and st.get("kind") == "shot_on" and st.get("out_dir")
             else st for st in steps]
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


def abs_out_dir(out_dir):
    """``out_dir`` as the broker must see it: absolute.

    The broker's cwd is not the caller's, so a relative directory would land
    somewhere else, and validate_job refuses a relative one. Resolved here, in
    the front-ends' shared builder, never in the broker.
    """
    if isinstance(out_dir, str) and out_dir:
        return os.path.abspath(os.path.expanduser(out_dir))
    return out_dir


def _shot_chunk(name, *, crop=True, frame=True, anchor=None, out_dir=None,
                marker=None, marker_timeout_s=None) -> Chunk:
    args = {"name": name, "crop": crop, "frame": frame, "anchor": anchor,
            "out_dir": abs_out_dir(out_dir), "settle_s": DEFAULT_SHOT_SETTLE_S}
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
                         "playback_ms": playback_ms,
                         "out_dir": abs_out_dir(out_dir),
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
    return [Chunk(ChunkKind.ENSURE, {}, expected_s=ENSURE_EXPECTED_S, max_s=ENSURE_MAX_S),
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


# ── Verdicts ─────────────────────────────────────────────────────────────────
# A finished job is not a passed one: the job completing only means its chunks
# ran. Whether a shot matched its anchor, a nav reached its screen or a test run
# passed is in the chunks' own results. The CLI turns that into an exit code
# and the MCP tools into `ok`/`error`; both read it here, so the two front-ends
# cannot drift apart again. Printing and JSON shaping stay with each front-end.
#
# The rules differ per command on purpose -- they are what each command has
# always answered, pinned in tests/test_jobs.py:
#   SHOT    (`shot`, `shot-on`, `wait`)  1 if the job failed; 3 when OCR ran and
#           an expected anchor was NOT on screen (None = skipped, not failed).
#   RECORD  1 if the job failed.
#   NAV     1 if the job failed; then the reach step's verdict: 3 for a
#           contradicted anchor, 1 for a gesture that did not land.
#   TEST    1 if the job failed, or if the test run itself said ok=false (no
#           verdict at all -- "Unable to connect", a crashed build, zero tests
#           -- comes back as a DONE job carrying ok=false).
#   OP      (`phone`, `close-aux`) 1 if the job failed.
#   JOB     the MCP tools: not ok when the job failed or any chunk said ok=false.
#   SCREEN  one row of `shots` / `ciqsim_shots`.
# `ok` is what an MCP tool reports; for NAV it also folds in JOB's any-chunk
# rule, which no CLI command applies.
SHOT, RECORD, NAV, TEST, OP, JOB, SCREEN = (
    "shot", "record", "nav", "test", "op", "job", "screen")
VERDICT_COMMANDS = frozenset({SHOT, RECORD, NAV, TEST, OP, JOB, SCREEN})

_TERMINAL = frozenset({JobState.DONE, JobState.FAILED, JobState.CANCELLED})


@dataclass(frozen=True)
class Verdict:
    ok: bool
    exit_code: int            # 0 passed · 1 failed · 3 an expected anchor was contradicted
    reason: str | None = None  # human text for the failure; None when there is none
    # A WAIT that timed out on a job that is still live. It keeps running and
    # will still write its output: resubmitting it is the wrong reaction.
    still_running: bool = False


def _step_results(result: dict):
    """(kind, result) of every chunk that ran; a null result reads as {}."""
    for step in (result.get("job") or {}).get("results") or []:
        yield step.get("kind"), step.get("result") or {}


def _reach_failure(result: dict) -> tuple[int, str] | None:
    for kind, res in _step_results(result):
        if kind != ChunkKind.REACH:
            continue
        if res.get("anchor") is False:
            return 3, (f"ran the steps but the target screen {res.get('screen')!r} "
                       f"did not show the expected text")
        if not res.get("ok"):
            return 1, res.get("error", "nav did not reach the screen")
    return None


def _test_failure(result: dict) -> str | None:
    for kind, res in _step_results(result):
        if kind == ChunkKind.TEST_BATCH and not res.get("ok"):
            why = res.get("reason") or (f"passed={res.get('passed')} "
                                        f"failed={res.get('failed')} "
                                        f"errors={res.get('errors')}")
            return f"tests did not pass ({why})"
    return None


def _anchor_contradicted(result: dict) -> bool:
    """True only when OCR ran and the expected text was NOT on a SHOT's screen.

    ``None`` means OCR was unavailable or read nothing: a skipped check, not a
    failed one -- failing there would break every machine without an OCR backend.
    """
    return any(kind == ChunkKind.SHOT and res.get("anchor") is False
               for kind, res in _step_results(result))


def _op_steps_failed(result: dict) -> bool:
    # A results entry is {"kind", "result"}, and reach nests its `steps` under
    # "result". The CLI used to read them one level up, where they never are, so
    # `phone`/`close-aux` exited 0 after a failed step. Both levels are read.
    for x in (result.get("job") or result).get("results") or []:
        if not isinstance(x, dict):
            continue
        for holder in (x, x.get("result")):
            steps = holder.get("steps") if isinstance(holder, dict) else None
            if isinstance(steps, list) and not all(
                    isinstance(st, dict) and st.get("ok") for st in steps):
                return True
    return False


def _screen_verdict(r: dict, job: dict, r_ok: bool) -> tuple[bool, str | None]:
    ok = r_ok
    err = job.get("error") or (r.get("error") if not r_ok else None)
    for kind, res in _step_results(r):
        if kind == ChunkKind.SHOT and res.get("anchor") is False:
            ok = False
            err = err or "expected anchor text was not on screen"
        elif kind == ChunkKind.REACH:
            if res.get("anchor") is False:
                ok = False
                err = err or "steps ran but the screen did not show the expected text"
            elif not res.get("ok"):
                ok = False
                err = err or res.get("error", "steps did not reach the screen")
    return ok, (None if ok else err or "failed")


def verdict(result: dict, command: str = JOB) -> Verdict:
    """The verdict on a WAIT reply ``result`` for ``command`` (see the table above)."""
    if command not in VERDICT_COMMANDS:
        raise ValueError(f"no verdict rule for {command!r}")
    r = result if isinstance(result, dict) else {}
    job = r.get("job") or {}
    r_ok = bool(r.get("ok"))
    still = (error_code(r) == E_WAIT_TIMEOUT
             and job.get("state") not in _TERMINAL)
    job_err = job.get("error") or r.get("error")
    any_step_failed = any(res.get("ok") is False for _, res in _step_results(r))

    if command == JOB:
        ok = r_ok and not any_step_failed
        return Verdict(ok, 0 if ok else 1, job_err, still)
    if command == SCREEN:
        ok, err = _screen_verdict(r, job, r_ok)
        return Verdict(ok, 0 if ok else 1, err, still)
    if command == NAV:
        fail = _reach_failure(r)
        code = 1 if not r_ok else (fail[0] if fail else 0)
        ok = r_ok and not any_step_failed and not fail
        return Verdict(ok, code, fail[1] if fail else job_err, still)
    if command == TEST:
        why = _test_failure(r)
        code = 0 if r_ok and not why else 1
        return Verdict(code == 0, code, why or (job_err if code else None), still)
    if command == SHOT:
        contradicted = _anchor_contradicted(r)
        code = 1 if not r_ok else (3 if contradicted else 0)
        reason = ("expected anchor text was not on screen" if contradicted
                  else job_err if code else None)
        return Verdict(code == 0, code, reason, still)
    if command == OP:
        code = 0 if r_ok and not _op_steps_failed(r) else 1
    else:                                   # RECORD
        code = 0 if r_ok else 1
    return Verdict(code == 0, code, job_err if code else None, still)
