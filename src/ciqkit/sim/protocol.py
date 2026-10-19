"""Wire protocol -- the shared vocabulary between the client and the broker.

JSON lines over a local stream socket (a unix socket, or authenticated
loopback TCP on Windows -- see :mod:`ciqkit.sim.transport`): each request and
response is one UTF-8 JSON object terminated by '\n'. Stdlib-only and fully JSON-serialisable, so the wire
format stays inspectable and the package stays dependency-free.

A JOB is an ordered list of CHUNKS plus scheduling metadata (priority, budgets,
owner). Chunks are the preemption unit: the scheduler may only switch jobs at a
chunk boundary, so every chunk must be individually short and restartable.
"""
from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import IntEnum


# ── Priorities ───────────────────────────────────────────────────────────────
# Lower value = more urgent (like `nice` inverted). A user "I want this now"
# maps to URGENT; normal agent screenshots to NORMAL; long regression runs to
# BATCH so anything interactive preempts them.
class Priority(IntEnum):
    URGENT = 0     # user-prioritized; preempts everything preemptible
    HIGH = 10      # interactive agent screenshot / nav
    NORMAL = 20    # default
    BATCH = 40     # long regression tests, bulk capture — yields to all above


# ── Chunk kinds ──────────────────────────────────────────────────────────────
# Each maps to one bounded SimSession operation. Kept coarse enough to be useful
# but fine enough that preemption between them is responsive.
class ChunkKind:
    ENSURE = "ensure"        # sim is up + paintable (launch if needed)
    SEED = "seed"            # patch source constants for a build-time profile
    LOAD = "load"            # build (if needed) + push + verify app is loaded
    ACT = "act"              # one verified tap/swipe/key (confirm transition)
    REACH = "reach"          # declarative nav recipe to a named screen
    SHOT = "shot"            # capture (+crop +optional OCR-anchor verify)
    RECORD = "record"        # burst-capture N seconds -> animated GIF + contact sheet
    TEST_BATCH = "test"      # run monkeydo -t for a filter; parse results
    RESTORE = "restore"      # undo seed / SEED_* (idempotent, always safe)


# SLOWEST median GIF frame duration for RECORD, in ms (~2 fps). A CEILING, not
# a target: the timing track is only ever sped UP to it. Capture normally puts
# frames ~220 ms apart, shorter than this, so a healthy clip passes through at
# its real wall-clock rate and this value never engages. It is the floor of watchability
# for a DEGRADED clip -- when grabs are slow, consecutive frames are distinct
# POSES rather than adjacent motion, and each needs ~a half second on screen to
# register as a step (the old 120 ms blurred them past the eye). Defined here,
# the dependency-free module both sides import, so the CLI, the MCP schema, the
# broker fallback and SimSession.record cannot drift apart.
DEFAULT_PLAYBACK_MS = 500
# Settle before a capture (shot, and the one-time pre-clip settle of a record).
# Here for the same reason: SimSession's defaults and the job builders in
# jobs.py both read it, so the CLI and MCP paths cannot disagree with it.
DEFAULT_SHOT_SETTLE_S = 0.35


@dataclass
class Chunk:
    kind: str
    args: dict = field(default_factory=dict)
    # Per-chunk soft/hard budgets (seconds). expected drives ETA; hard kills.
    expected_s: float = 15.0
    max_s: float = 120.0

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> Chunk:
        return Chunk(
            kind=d["kind"],
            args=d.get("args", {}) or {},
            expected_s=float(d.get("expected_s", 15.0)),
            max_s=float(d.get("max_s", 120.0)),
        )


class JobState:
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"          # preempted at a chunk boundary; will resume
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    app: str                          # app id (folderName) the job targets
    chunks: list                      # list[Chunk]
    priority: int = Priority.NORMAL
    device: str = "venu3"
    owner: str = ""                   # session id -- who submitted (visibility)
    label: str = ""                   # human tag for status output
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    # Runtime (server-side) fields — not required from the client.
    state: str = JobState.QUEUED
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    chunk_ix: int = 0                 # next chunk to run (resume point on preempt)
    results: list = field(default_factory=list)   # per-chunk result dicts
    error: str | None = None
    # Aging clock. `waiting_since` is when the current waiting spell began, or
    # None while the job holds the simulator; `waited_total` is the seconds
    # accumulated over previous spells. Aging is measured from these rather
    # than from `created`, so time spent RUNNING never promotes a job past the
    # work it is making wait.
    waiting_since: float | None = None
    waited_total: float = 0.0

    # Effective priority: ages toward more-urgent the longer a job WAITS, so a
    # flood of urgent work cannot starve a batch job forever. One tier per
    # AGING_S seconds waited.
    AGING_S = 45.0
    # Aging must never reach URGENT itself. Flooring at URGENT (0) made every
    # job older than 4 x AGING_S compare equal, so `_runnable`'s tie-break fell
    # to `created` and the OLDEST job won -- i.e. after three minutes a batch
    # regression run outranked a freshly submitted urgent screenshot and the
    # headline "urgent preempts everything preemptible" silently stopped
    # holding. AGING_FLOOR keeps an aged job strictly below a real URGENT while
    # still letting it overtake HIGH and NORMAL, which is all anti-starvation
    # needs.
    AGING_FLOOR = float(Priority.URGENT) + 1.0

    def effective_priority(self, now: float | None = None) -> float:
        """Priority adjusted for how long this job has been waiting to run.

        Ages from `waiting_since`, not `created`: the clock is about queueing
        delay, and a job that is running is not being starved. Counting run
        time too let a long batch job promote ITSELF while it held the
        simulator, which is the opposite of what aging is for.
        """
        if self.priority <= Priority.URGENT:
            return float(self.priority)     # already at the top; nothing to age
        now = now if now is not None else time.time()
        # Whole tiers only. A continuous ramp meant a job that had waited a
        # single second outranked the one currently holding the simulator, so
        # equal-priority jobs ping-ponged at every chunk boundary -- and each
        # switch costs an app reload. Stepping makes "one tier per AGING_S"
        # literally true and keeps same-tier jobs exactly equal until one has
        # genuinely waited a tier's worth, so `_runnable`'s created tie-break
        # still gives FIFO within a tier.
        tiers = int(self.waited_s(now) // self.AGING_S)
        aged = self.priority - tiers * 10.0
        return max(self.AGING_FLOOR, aged)

    def waited_s(self, now: float | None = None) -> float:
        """Seconds this job has spent QUEUED or PAUSED, never RUNNING."""
        total = self.waited_total
        if self.waiting_since is not None:
            now = now if now is not None else time.time()
            total += max(0.0, now - self.waiting_since)
        return total

    def mark_running(self, now: float | None = None) -> None:
        """Called by the scheduler when this job takes the simulator.

        Banks the current waiting spell and stops the clock: time spent
        RUNNING is not time spent waiting.
        """
        if self.waiting_since is not None:
            now = now if now is not None else time.time()
            self.waited_total += max(0.0, now - self.waiting_since)
            self.waiting_since = None

    def mark_waiting(self, now: float | None = None) -> None:
        """Called when the job is queued or preempted back into the queue."""
        if self.waiting_since is None:
            self.waiting_since = now if now is not None else time.time()

    def expected_total_s(self) -> float:
        return sum(c.expected_s for c in self.chunks[self.chunk_ix:])

    def to_dict(self) -> dict:
        d = asdict(self)
        d["chunks"] = [c.to_dict() if isinstance(c, Chunk) else c for c in self.chunks]
        return d

    # Fields a CLIENT may set. Everything else on this dataclass is the
    # daemon's own bookkeeping and is deliberately not readable from the wire
    # -- see :meth:`from_dict`.
    CLIENT_FIELDS = frozenset({"app", "chunks", "priority", "device", "owner",
                               "label", "id"})

    @staticmethod
    def from_dict(d: dict) -> Job:
        """Build a submittable Job from a client's request.

        ONLY the client-owned fields are read. `state`, `chunk_ix`, `results`,
        `created`, `started`, `finished` and `error` are the broker's own
        bookkeeping and are ignored, however the caller spells them.

        They used to be carried through "if present (daemon status echoes)",
        which made a status dict look like a valid submission -- and echoing
        one back is the obvious way to retry a job. The consequences were not
        subtle:

          * `state: "done"` plus a fabricated `results` list produced a job the
            scheduler skipped as already-terminal, so WAIT returned ok=True
            with a screenshot path that was never captured. The simulator was
            never touched.
          * `chunk_ix: 2` skipped the job's own SEED and LOAD chunks, so a
            recording of the UNSEEDED app came back reported as seeded.

        A confidently wrong answer is the one failure mode this project exists
        to prevent, so the wire simply cannot express these.
        """
        chunks = [Chunk.from_dict(c) for c in d.get("chunks", [])]
        return Job(
            app=d["app"],
            chunks=chunks,
            priority=int(d.get("priority", Priority.NORMAL)),
            device=d.get("device", "venu3"),
            owner=d.get("owner", ""),
            label=d.get("label", ""),
            id=d.get("id") or uuid.uuid4().hex[:12],
        )


# ── Submit-time validation ───────────────────────────────────────────────────
# What each chunk kind cannot run without. Checked when the broker ACCEPTS a
# job, not when the chunk runs: a SHOT missing its name used to surface as a
# KeyError only after the job's LOAD had spent minutes building and holding the
# simulator. A tuple entry means "at least one of these".
CHUNK_REQUIRED: dict[str, tuple] = {
    ChunkKind.ENSURE: (),
    ChunkKind.SEED: ("app_dir",),
    ChunkKind.RESTORE: (),
    ChunkKind.LOAD: (("prg", "app_dir"),),
    ChunkKind.ACT: (),
    ChunkKind.REACH: (),
    ChunkKind.SHOT: ("name",),
    ChunkKind.RECORD: ("name",),
    ChunkKind.TEST_BATCH: (("prg", "app_dir"),),
}
# Ceiling on any one chunk's budget. A chunk is the preemption unit, so one
# that may legitimately run for over an hour is a design error, not a budget.
MAX_CHUNK_S = 3600.0


def _budget_error(where: str, name: str, v: float) -> str | None:
    # json.loads accepts NaN and 1e400. A NaN max_s reached Thread.join() on
    # the scheduler thread, raised there, and took the whole broker -- and
    # every queued job -- down with it.
    if not math.isfinite(v) or not 0 < v <= MAX_CHUNK_S:
        return f"{where}: {name}={v!r} must be a finite number in (0, {MAX_CHUNK_S:.0f}]"
    return None


def validate_priority(p) -> str | None:
    """Why ``p`` is not a usable priority, or None."""
    if isinstance(p, bool) or not isinstance(p, int) or not (
            Priority.URGENT <= p <= Priority.BATCH):
        return (f"priority={p!r} must be an integer from {int(Priority.URGENT)} "
                f"(urgent) to {int(Priority.BATCH)} (batch)")
    return None


# ── Step schema ──────────────────────────────────────────────────────────────
# One vocabulary for a gesture/op whether it arrives as a nav-recipe step
# (REACH) or as an ACT chunk's args. ``button`` names a physical key: ``id`` is
# canonical (what nav steps and the device's simulator.json call it), ``button``
# is an accepted alias.
STEP_KINDS = ("tap", "hold", "swipe", "key", "button", "close_aux", "phone",
              "fit", "shot_on")
PHONE_VALUES = ("connected", "disconnected")


def button_id(step: dict) -> str | None:
    """The physical-key id of a step, from ``id`` or the ``button`` alias."""
    v = step.get("id")
    return v if v not in (None, "") else (step.get("button") or None)


SWIPE_DIRECTIONS = ("up", "down", "left", "right")
#: Where an ``edge`` swipe starts (display fractions) and which way it pulls.
#: ``edge: "right"`` is a swipe in from the right edge, i.e. toward the left.
EDGE_SWIPES = {"left": ((0.03, 0.5), "right"), "right": ((0.97, 0.5), "left"),
               "top": ((0.5, 0.03), "down"), "bottom": ((0.5, 0.97), "up")}
EDGE_DIST = 0.4


def swipe_spec(step: dict) -> dict | None:
    """The explicit-geometry form of a swipe step, or None for the default swipe.

    ``from: [x, y]`` (display fractions) starts the drag there and ``dist`` is
    its length as a fraction of the display width (default 0.4 with ``from``).
    ``edge: left|right|top|bottom`` is the convenience form: start at that
    edge, pull inward. Any of them selects this form; a bare ``{"kind":
    "swipe", "direction": ...}`` keeps the default centred flick (``None``).
    Returns ``{"from": (x, y), "direction": d, "dist": f}``. Assumes the step
    passed :func:`step_errors`.
    """
    edge, frm, dist = step.get("edge"), step.get("from"), step.get("dist")
    if edge is None and frm is None and dist is None:
        return None
    direction = step.get("direction")
    if edge is not None:
        e_from, e_dir = EDGE_SWIPES[edge]
        frm = frm if frm is not None else e_from
        direction = direction or e_dir
    return {"from": (float(frm[0]), float(frm[1])) if frm is not None else (0.5, 0.5),
            "direction": direction or "up",
            "dist": float(dist) if dist is not None else EDGE_DIST}


def _swipe_errors(step, where) -> list[str]:
    errs = []
    d = step.get("direction")
    if d is not None and d not in SWIPE_DIRECTIONS:
        errs.append(f"{where}: swipe direction {d!r} must be one of "
                    f"{', '.join(SWIPE_DIRECTIONS)}")
    e = step.get("edge")
    if e is not None and e not in EDGE_SWIPES:
        errs.append(f"{where}: swipe edge {e!r} must be one of "
                    f"{', '.join(EDGE_SWIPES)}")
    f = step.get("from")
    if f is not None:
        ok = (isinstance(f, (list, tuple)) and len(f) == 2
              and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                      and math.isfinite(v) and 0.0 <= v <= 1.0 for v in f))
        if not ok:
            errs.append(f"{where}: swipe from must be [x, y] display fractions "
                        f"in 0..1, got {f!r}")
    dist = step.get("dist")
    if dist is not None and (isinstance(dist, bool) or not isinstance(dist, (int, float))
                             or not math.isfinite(dist) or not 0.0 < dist <= 1.5):
        errs.append(f"{where}: swipe dist must be a number in (0, 1.5] (fraction "
                    f"of the display width), got {dist!r}")
    return errs


def step_errors(step, where: str = "step") -> list[str]:
    """Everything wrong with one gesture/op, empty when it is runnable.

    The single validator for REACH steps and ACT args, so the two paths cannot
    disagree about the schema again (ACT used to take only ``button``, nav only
    ``id``).
    """
    if not isinstance(step, dict):
        return [f"{where}: must be an object"]
    kind = step.get("kind", "tap")
    errs = []
    if kind not in STEP_KINDS:
        errs.append(f"{where}: unknown kind {kind!r}; expected one of "
                    f"{', '.join(STEP_KINDS)}")
    a, b = step.get("id"), step.get("button")
    if a not in (None, "") and b not in (None, "") and a != b:
        errs.append(f"{where}: id={a!r} and button={b!r} disagree; pass one "
                    f"(id is canonical)")
    if kind == "button" and not button_id(step):
        errs.append(f"{where}: kind 'button' needs an id (a key from the device's "
                    f"simulator.json: enter, esc, up, down, menu, ...)")
    if kind == "swipe":
        errs.extend(_swipe_errors(step, where))
    if kind == "fit" and not (isinstance(step.get("path"), str) and step.get("path")):
        errs.append(f"{where}: kind 'fit' needs a path (the .fit/.gpx file to play back)")
    if kind == "shot_on":
        if not (isinstance(step.get("marker"), str) and step.get("marker")):
            errs.append(f"{where}: kind 'shot_on' needs a marker (console text to wait for)")
        if not (isinstance(step.get("name"), str) and step.get("name")):
            errs.append(f"{where}: kind 'shot_on' needs a name (the PNG's file name)")
        t = step.get("timeout", 20)
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 < t <= 600:
            errs.append(f"{where}: shot_on timeout must be a number in (0, 600] seconds")
    if kind == "phone":
        v = step.get("state", step.get("value"))
        if str(v).lower() not in PHONE_VALUES:
            errs.append(f"{where}: kind 'phone' needs state "
                        f"{' or '.join(PHONE_VALUES)}, got {v!r}")
    return errs


def validate_job(job: Job) -> list[str]:
    """Everything wrong with a submitted job, empty when it can be queued.

    Stdlib-only and side-effect free so the CLI could call it too; the broker
    calls it before the job ever reaches the table. A job that fails here is
    refused with every reason at once, rather than accepted and then failed
    one chunk at a time.
    """
    errs = []
    if not job.chunks:
        # Zero chunks is never runnable and never terminal: its WAIT hung to
        # the timeout and it kept the broker from ever idling out.
        errs.append("job has no chunks")
    if (e := validate_priority(job.priority)) is not None:
        errs.append(e)
    for i, c in enumerate(job.chunks):
        where = f"chunk {i} ({c.kind!r})"
        req = CHUNK_REQUIRED.get(c.kind)
        if req is None:
            errs.append(f"{where}: unknown chunk kind; expected one of "
                        f"{', '.join(sorted(CHUNK_REQUIRED))}")
            continue
        if not isinstance(c.args, dict):
            errs.append(f"{where}: args must be an object")
            continue
        for key in req:
            alts = key if isinstance(key, tuple) else (key,)
            if not any(c.args.get(a) for a in alts):
                errs.append(f"{where}: missing {' or '.join(alts)}")
        if c.kind == ChunkKind.ACT:
            errs.extend(step_errors(c.args, where))
        elif c.kind == ChunkKind.LOAD and c.args.get("fit") is not None:
            if not (isinstance(c.args["fit"], str) and c.args["fit"]):
                errs.append(f"{where}: fit must be the path of a .fit/.gpx file")
        elif c.kind == ChunkKind.SHOT and c.args.get("marker") is not None:
            if not (isinstance(c.args["marker"], str) and c.args["marker"]):
                errs.append(f"{where}: marker must be a non-empty string")
            t = c.args.get("marker_timeout_s", 20)
            if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 < t <= 600:
                errs.append(f"{where}: marker_timeout_s must be a number in (0, 600]")
        elif c.kind == ChunkKind.REACH:
            steps = c.args.get("steps") or []
            if not isinstance(steps, list):
                errs.append(f"{where}: steps must be a list")
            else:
                for j, st in enumerate(steps):
                    errs.extend(step_errors(st, f"{where} step {j + 1}"))
        for name in ("expected_s", "max_s"):
            if (e := _budget_error(where, name, getattr(c, name))) is not None:
                errs.append(e)
    return errs


# ── Request / response envelopes ─────────────────────────────────────────────
# Verbs the client sends. One JSON object per line.
class Op:
    SUBMIT = "submit"      # {op, job}                       -> {ok, job_id}
    WAIT = "wait"          # {op, job_id, timeout_s}         -> {ok, job} (final)
    STATUS = "status"      # {op}                            -> {ok, jobs:[...], current}
    BUMP = "bump"          # {op, job_id, priority}          -> {ok}
    CANCEL = "cancel"      # {op, job_id}                    -> {ok}
    PING = "ping"          # {op}                            -> {ok, pid, uptime_s}
    SHUTDOWN = "shutdown"  # {op}                            -> {ok}  (admin/tests)


# What a broker that has been told to stop answers a SUBMIT with. A client that
# sees it waits for that broker to exit and submits to a fresh one.
ERR_SHUTTING_DOWN = "broker is shutting down"


def dumps(obj: dict) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


def loads(line: bytes) -> dict:
    return json.loads(line.decode("utf-8"))


# Default socket + runtime dir.
#
# The socket is a COMMAND CHANNEL: a job can build a project and patch its
# source files. It therefore lives in a private, per-user, 0700 directory (see
# ciqkit.paths) so no other local account can submit jobs to it.
def runtime_dir() -> str:
    from ciqkit.paths import runtime_dir as _rt
    return _rt()


def socket_path() -> str:
    """The broker's endpoint: a unix socket, or on Windows the private file
    naming its loopback port and token (see :mod:`ciqkit.sim.transport`)."""
    import os
    override = os.environ.get("CIQSIM_SOCK")
    if override:
        return override
    from .transport import default_endpoint_name
    return os.path.join(runtime_dir(), default_endpoint_name())
