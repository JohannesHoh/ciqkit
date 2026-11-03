"""Fuzz properties, shared by the Atheris harnesses and the pytest replay.

Each ``check_*`` takes the fuzzer's bytes and raises AssertionError (or lets
an unexpected exception through) when a property breaks. Stdlib only, so
``tests/test_fuzz_targets.py`` runs the same properties with a fixed seed on
every platform, where Atheris (Linux-only wheels) is not installed.

The targets are the places untrusted text enters:

* the control channel: one JSON line -> ``Job.from_dict`` -> ``validate_job``,
  exactly as ``broker.handle`` does for SUBMIT;
* the simulator test harness's stdout -> ``recording._parse_test_output``.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable

from ciqkit.sim import protocol as proto
from ciqkit.sim.protocol import CHUNK_REQUIRED, Job, JobState
from ciqkit.sim.recording import _legacy_counts, _parse_test_output

# ── Structured input ─────────────────────────────────────────────────────────
# Raw bytes almost never form a JSON job, so most of the search would stop at
# json.loads. Both drivers instead hand a `pick(n) -> int in [0, n)` source to
# build_job(), which assembles a job from the real vocabulary plus wrong types
# in every slot. Atheris feeds `pick` from FuzzedDataProvider (so its coverage
# feedback steers the choices); the pytest replay feeds it from random.Random.

Pick = Callable[[int], int]

_WORDS = ("", "a", "venu3", "x" * 40, "../etc", "ensure", "seed", "restore",
          "load", "act", "reach", "shot", "record", "tap", "swipe", "button",
          "phone", "fit", "shot_on", "up", "left", "right", "top", "connected",
          "name", "app_dir", "prg", "steps", "marker", "edge", "from", "dist",
          "NaN", "1e400", "done", "PASSED")
_NUMBERS = (0, 1, -1, 2, 3, 0.5, 1.5, 600, 3601, 1e308, float("inf"),
            float("nan"), 10**18)
_KEYS = ("kind", "args", "expected_s", "max_s", "name", "app_dir", "prg",
         "steps", "direction", "edge", "from", "dist", "id", "button", "path",
         "marker", "timeout", "marker_timeout_s", "state", "value", "fit")
_SERVER = {"state": JobState.DONE, "chunk_ix": 2, "results": [{"path": "/fake.png"}],
           "created": 1.0, "started": 2.0, "finished": 3.0, "error": "x",
           "waiting_since": 0.0, "waited_total": 1e6}


def build_value(pick: Pick, depth: int = 0):
    """Any JSON value, biased toward the words and numbers the schema uses."""
    k = pick(8 if depth < 3 else 5)
    if k == 0:
        return None
    if k == 1:
        return bool(pick(2))
    if k == 2:
        return _NUMBERS[pick(len(_NUMBERS))]
    if k in (3, 4):
        return _WORDS[pick(len(_WORDS))]
    if k == 5:
        return [build_value(pick, depth + 1) for _ in range(pick(4))]
    if k == 6:
        return build_dict(pick, depth + 1)
    return [_NUMBERS[pick(len(_NUMBERS))], _NUMBERS[pick(len(_NUMBERS))]]


def build_dict(pick: Pick, depth: int = 0) -> dict:
    return {_KEYS[pick(len(_KEYS))]: build_value(pick, depth) for _ in range(pick(5))}


_STEP_KEYS = ("id", "button", "direction", "edge", "from", "dist", "path",
              "marker", "name", "timeout", "state", "value")


def build_step(pick: Pick):
    """A gesture: a real step kind plus a few keys of any type."""
    step = {"kind": (proto.STEP_KINDS[pick(len(proto.STEP_KINDS))] if pick(5)
                     else build_value(pick, 2))}
    for _ in range(pick(4)):
        step[_STEP_KEYS[pick(len(_STEP_KEYS))]] = build_value(pick, 2)
    return step


def build_args(pick: Pick, kind) -> dict:
    if kind == "act" and pick(3):
        return build_step(pick)
    if kind == "reach" and pick(3):
        return {"steps": [build_step(pick) for _ in range(pick(4))]}
    return build_dict(pick, 1)


def build_chunk(pick: Pick):
    if pick(10) == 0:
        return build_value(pick, 2)               # not even an object
    c = {"kind": (list(CHUNK_REQUIRED)[pick(len(CHUNK_REQUIRED))] if pick(4)
                  else build_value(pick, 2))}
    if pick(4):
        c["args"] = build_args(pick, c["kind"]) if pick(5) else build_value(pick, 2)
    for budget in ("expected_s", "max_s"):
        if pick(3) == 0:
            c[budget] = build_value(pick, 3)
    return c


def build_job(pick: Pick) -> dict:
    job: dict = {"app": "app" if pick(4) else build_value(pick, 2)}
    job["chunks"] = ([build_chunk(pick) for _ in range(pick(5))] if pick(8)
                     else build_value(pick, 2))
    for name in ("priority", "device", "owner", "label", "id"):
        if pick(3) == 0:
            job[name] = build_value(pick, 2)
    if pick(2):                                   # a status echo, the classic retry
        job.update({k: v for k, v in _SERVER.items() if pick(2)})
    return job


# ── Control channel ──────────────────────────────────────────────────────────

def _assert_server_fields_default(job: Job) -> None:
    # The wire cannot express a finished job (CLAUDE.md invariant).
    assert job.state == JobState.QUEUED, job.state
    assert job.chunk_ix == 0 and job.results == [] and job.error is None
    assert job.started is None and job.finished is None
    assert job.waiting_since is None and job.waited_total == 0.0


def _assert_well_typed(job: Job) -> None:
    """What `validate_job() == []` promises the scheduler and the session."""
    for name in ("app", "device", "owner", "label", "id"):
        assert isinstance(getattr(job, name), str), (name, getattr(job, name))
    assert proto.validate_priority(job.priority) is None
    assert job.chunks
    for c in job.chunks:
        assert isinstance(c.kind, str) and c.kind in CHUNK_REQUIRED, c.kind
        assert isinstance(c.args, dict)
        for key in CHUNK_REQUIRED[c.kind]:
            alts = key if isinstance(key, tuple) else (key,)
            assert any(isinstance(c.args.get(a), str) and c.args.get(a) for a in alts)
        for b in (c.expected_s, c.max_s):
            assert math.isfinite(b) and 0 < b <= proto.MAX_CHUNK_S


def check_submit(job_dict) -> None:
    """One SUBMIT body through the broker's own parse-then-validate path."""
    try:
        job = Job.from_dict(job_dict)
    except Exception:  # noqa: BLE001
        # broker.handle's caller turns any Exception into {ok: False, error}.
        # A refusal is fine; what must not happen is the cases below.
        return
    _assert_server_fields_default(job)
    errs = proto.validate_job(job)           # must report, never raise
    assert isinstance(errs, list)
    if errs:
        return
    _assert_well_typed(job)
    # What the broker accepted survives its own status echo unchanged.
    again = Job.from_dict(proto.loads(proto.dumps(job.to_dict())))
    for name in Job.CLIENT_FIELDS - {"chunks"}:
        assert getattr(again, name) == getattr(job, name), name
    # Compared serialised: free-form args may hold NaN, and NaN != NaN.
    assert (proto.dumps({"c": [c.to_dict() for c in again.chunks]})
            == proto.dumps({"c": [c.to_dict() for c in job.chunks]}))
    assert proto.validate_job(again) == []


def check_wire_line(data: bytes) -> None:
    """A raw control-channel line, as the connection handler reads it."""
    try:
        req = proto.loads(data)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return                                  # refused by the handler's except
    if isinstance(req, dict) and "job" in req:
        check_submit(req["job"])


# ── Test-harness output ──────────────────────────────────────────────────────

# The single regex _parse_test_output used before it went linear. Kept here so
# the replay can prove the two agree; quadratic, so only fed short inputs.
_OLD_LEGACY = re.compile(r"Passed[:\s]+(\d{1,9}).*?Failed[:\s]+(\d{1,9})", re.S | re.I)
#: Generous: a linear parse of this much text takes milliseconds.
PARSE_BUDGET_S = 0.5


def check_test_output(text: str) -> None:
    t0 = time.perf_counter()
    r = _parse_test_output(text)
    took = time.perf_counter() - t0
    assert took < PARSE_BUDGET_S, f"{len(text)} chars took {took:.2f}s"
    assert isinstance(r["ok"], bool)
    assert r["out"] == text[-2000:]
    for k in ("passed", "failed", "errors", "ran"):
        if k in r:
            assert isinstance(r[k], int) and r[k] >= 0, (k, r[k])
    if r["ok"]:
        # A pass needs tests that ran and none that failed.
        assert r["ran"] > 0 and r["failed"] == 0 and r["errors"] == 0
    if len(text) <= 4096:
        old = _OLD_LEGACY.search(text)
        want = None if old is None else (int(old.group(1)), int(old.group(2)))
        assert _legacy_counts(text) == want, (want, _legacy_counts(text))


def build_test_output(pick: Pick) -> str:
    """Harness-shaped text: summary fragments in random order and spacing."""
    parts = ("Passed", "PASSED", "Failed", "FAILED", "passed=", "failed=",
             "errors=", "Ran ", " tests", "(", ")", ",", ": ", " ", "\n", "\t",
             "1", "0", "12", "999999999", "1234567890", "x",
             "Unable to connect to simulator")
    return "".join(parts[pick(len(parts))] for _ in range(pick(60)))


def check_test_output_bytes(data: bytes) -> None:
    check_test_output(data.decode("utf-8", "replace"))

