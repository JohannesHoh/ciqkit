"""The broker daemon.

Single machine-wide owner of one SimSession. A background scheduler thread runs
queued and paused jobs one CHUNK at a time; because it re-selects the best job
at every chunk boundary, a higher-priority job that arrives mid-chunk preempts
the running one as soon as that chunk finishes. The displaced job flips to
PAUSED and resumes later; if an intervening job pushed another app, it rewinds
to its own LOAD and replays from there (see Broker._rewind_locked).

Threading:
  * one scheduler thread drives the SimSession (the single-actor invariant);
  * a threaded unix-socket server accepts client ops and mutates the job table
    under a single lock and condition variable.

Against the outside world: while the queue is non-empty the daemon holds the
machine-wide `sim` lease (:mod:`ciqkit.leases`) and releases it when idle, so
scripts that drive the simulator without going through the broker still
serialise against it.

The control socket is a command channel -- see SECURITY.md and
:func:`_bind_private`.
"""
from __future__ import annotations

import dataclasses
import logging
import math
import os
import socket
import sys
import threading
import time
import traceback

from . import preflight
from . import protocol as proto
from . import seedjournal
from . import transport
from .._compat import open_lock_file, try_lock
from .protocol import Job, Chunk, ChunkKind, JobState, Op
from .session import SimSession, SimError
from .. import leases as leaselib

IDLE_EXIT_S = float(os.environ.get("CIQSIM_IDLE_EXIT_S", "600"))   # 10 min
SIM_LEASE_TIMEOUT_S = float(os.environ.get("CIQSIM_SIM_LEASE_TIMEOUT_S", "180"))
# How long to wait for a timed-out chunk's worker thread to actually stop before
# giving up on it and running the next job anyway. See Broker._reap_zombie.
ZOMBIE_GRACE_S = float(os.environ.get("CIQSIM_ZOMBIE_GRACE_S", "60"))
# How long a finished job stays visible in `sim status` / answerable by WAIT.
# Comfortably longer than any client wait timeout, so re-attaching to a job that
# just finished still works; without it the in-memory table only ever grows.
JOB_RETENTION_S = float(os.environ.get("CIQSIM_JOB_RETENTION_S", "900"))
# How long a SHUTDOWN waits for the chunk in flight to reach its boundary before
# the process exits anyway (killing it mid-chunk). Under Client.shutdown's own
# 30 s wait, so `ciqkit sim shutdown` still sees the broker go.
SHUTDOWN_GRACE_S = float(os.environ.get("CIQSIM_SHUTDOWN_GRACE_S", "20"))
# How long restores may keep failing before the jobs the stuck seed gates are
# failed with the restore error instead of queueing behind it for ever. Time,
# not a count of attempts: the first retry runs on the very next loop round, so
# "three attempts" was over within a few seconds -- too short for the transient
# causes (an editor or indexer holding the file, a sync client) it should ride
# out. Retries back off from RESTORE_RETRY_MIN_S up to RESTORE_RETRY_MAX_S and
# go on after the give-up; only the waiting jobs are released.
RESTORE_GIVE_UP_S = 30.0
RESTORE_RETRY_MIN_S = 1.0
RESTORE_RETRY_MAX_S = 10.0
TERMINAL = {JobState.DONE, JobState.FAILED, JobState.CANCELLED}
# Chunks that read or drive the screen, so need this job's app (and the screen
# its earlier chunks navigated to) up when they start.
SCREEN_KINDS = (ChunkKind.ACT, ChunkKind.SHOT, ChunkKind.REACH, ChunkKind.RECORD)
# Concurrent client connections. Each gets a thread that lives as long as the
# client holds the socket open, so this is a ceiling on threads, not on
# throughput -- every op is fast and the queue is what actually serialises work.
MAX_CONNS = int(os.environ.get("CIQSIM_MAX_CONNS", "64"))
_CONN_SLOTS = threading.BoundedSemaphore(MAX_CONNS)
# Longest request line a connection may send. Real requests are a few KiB; a
# peer that never sends a newline would otherwise grow the handler's buffer
# without bound for the whole hour the socket may stay open.
MAX_LINE = 1024 * 1024
# What a build (and, in TEST_BATCH, the test run after it) is given LESS than
# the chunk's max_s. _run_chunk joins the worker for exactly max_s, so a call
# handed the full max_s always loses that race: the job then fails as "chunk
# exceeded max_s" and the simulator is restarted to free a worker that was only
# waiting on monkeyc. With the margin the call's own timeout fires first and
# the job fails cleanly as "build timed out".
#
# Both calls go through _compat.run_killing_tree, which on a timeout kills the
# whole process tree, then gives the output and the reap kill_grace_s (2 s)
# each, so a call overruns its own timeout by a few seconds at worst. (Plain subprocess.run killed
# only the Git Bash / cmd.exe wrapper and, on Windows, then waited unbounded
# on pipes monkeyc's java still held -- which no margin could bound; this was
# 30 s there.) Windows keeps a little more than POSIX: process teardown and
# the Toolhelp tree walk are slower there, and that has not been measured on a
# real build yet.
BUDGET_MARGIN_S = 10.0 if sys.platform == "win32" else 5.0

log = logging.getLogger("ciqkit.sim.broker")


def _bad(what: str) -> dict:
    return proto.error_reply(proto.E_BAD_REQUEST, f"bad request: {what}")


class Broker:
    def __init__(self, session=None):
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        # Injectable so tests hand in a fake instead of patching it in after.
        self.session = session if session is not None else SimSession()
        self._stop = False
        self._last_activity = time.time()
        self._sim_lease_cm = None       # held while busy
        self._last_job_id = None        # for preempt bookkeeping
        self._zombie = None             # worker thread of a timed-out chunk
        self._seed_job_id = None        # job whose SEED patch is currently applied
        self._seed_app_dir = None       # realpath of the tree that patch is in
        # Bumped by every chunk that replaces the app running in the simulator
        # (LOAD, TEST_BATCH, reloads). A job remembers the value right after its
        # own LOAD; any other value means somebody pushed since. See
        # _reload_chunk for why "is the same checkout loaded" is not enough.
        self._load_gen = 0
        self._job_load_gen: dict[str, int] = {}
        self._requests_in_flight = 0    # socket ops between read and reply
        self._restore_pending = False   # a cancel asked for a restore
        self._restore_failures = 0      # consecutive failed restores
        self._restore_error = ""        # the last one's message
        self._restore_failing_since = None  # monotonic time of the first of them
        self._restore_next_at = 0.0     # monotonic; no retry before this (backoff)
        # A restore worker that outlived ZOMBIE_GRACE_S. Teardown must not
        # start a second restore beside it; the seed journal recovers the file.
        self._restore_wedged = None
        # Set when the broker stops with a seed patch it could not restore:
        # WAIT then answers ok=False naming the patch, instead of waiting on a
        # restore that will never run here.
        self._restore_abandoned = ""

    # ── job selection ─────────────────────────────────────────────────────────
    def _runnable(self):
        """Best job to run next: min effective priority among non-terminal jobs
        that still have chunks left. Ties -> earliest created."""
        now = time.time()
        best = None
        best_key = None
        for j in self.jobs.values():
            if j.state in TERMINAL:
                continue
            if j.chunk_ix >= len(j.chunks):
                continue
            if self._blocked_by_seed(j):
                continue
            key = (j.effective_priority(now), j.created)
            if best is None or key < best_key:
                best, best_key = j, key
        return best

    def _blocked_by_seed(self, j: Job) -> bool:
        """Must ``j`` wait because ANOTHER job's seed is patched into the tree?

        A seed is not chunk-local: it stays in the user's source from its SEED
        chunk to its RESTORE, across chunk boundaries -- i.e. across
        preemptions. A job that builds that tree in between compiles the other
        job's SEED_* values into its own app and reports the screenshot as
        verified; a job that seeds in between fails outright, because only one
        seed can be active. So while a seed is live, those jobs sit out until
        its owner restores. Everything else -- a different project, a pre-built
        .prg, a gesture -- still preempts as usual, so the urgent path only
        waits when it would otherwise have produced a wrong answer.
        """
        if self._seed_job_id is not None and self._seed_job_id == j.id:
            return False
        # A terminal owner releases the gate only once its patch is really out
        # of the tree. A CANCEL (or a timed-out chunk) merely raises
        # _restore_pending and the scheduler restores later; a restore that
        # raised leaves it raised. Until then the seed is live whatever the
        # owner's state -- or whether it has been reaped from the table.
        #
        # That includes a patch with NO owner: a SEED chunk cancelled or timed
        # out in flight may have patched the tree without ever being recorded
        # as owner (see _run_chunk). While its restore is pending -- or keeps
        # failing -- the gate stays up on the tree it named (_seed_app_dir),
        # or on every built tree if even that is unknown.
        if not self._restore_pending:
            if self._seed_job_id is None:
                return False
            owner = self.jobs.get(self._seed_job_id)
            if owner is None or owner.state in TERMINAL:
                return False
        # The chunk it will really run: a job about to SHOT whose app was
        # pushed over rewinds to its LOAD and builds. Gating only on the
        # literal next chunk let a bumped job rebuild the seeded tree.
        nxt = j.chunks[self._rewind_ix(j)]
        if nxt.kind == ChunkKind.SEED:
            return True
        if nxt.kind in (ChunkKind.LOAD, ChunkKind.TEST_BATCH):
            a = nxt.args or {}
            if not a.get("prg") and a.get("app_dir"):
                return (self._seed_app_dir is None
                        or _realdir(a["app_dir"]) == self._seed_app_dir)
        return False

    def _non_terminal(self):
        return [j for j in self.jobs.values() if j.state not in TERMINAL]

    # ── scheduler loop ────────────────────────────────────────────────────────
    def scheduler(self):
        """Drive the queue until stopped.

        The loop body is wrapped in a catch-all because this thread IS the
        broker: if it dies, the accept loop keeps taking jobs that will never
        run, WAIT times out on jobs stuck QUEUED, idle-exit never fires, and
        the machine-wide `sim` lease is never released. A daemon that is up but
        does nothing is worse than one that is down -- so an unexpected error
        fails whatever job is current, and a truly unrecoverable one takes the
        process down where the client can see it.

        BaseException, not Exception, for the same reason the chunk worker uses
        it: the capture layer historically raised SystemExit, which an
        `except Exception` guard lets straight through.
        """
        try:
            self._scheduler_loop()
            # A stop (SHUTDOWN, idle-exit, a wedged worker) leaves the loop at a
            # chunk boundary. Anything still live will never run in this
            # process; say so, rather than let its WAIT end in a dropped socket.
            self._fail_live("the broker shut down before this job finished; "
                            "re-submit it")
        except BaseException:   # noqa: BLE001 -- see docstring
            # log.exception already writes the full traceback to brokerd.log,
            # the documented diagnostic channel; re-raising would only print a
            # duplicate through the threading excepthook.
            log.exception("the scheduler thread died; shutting the broker down "
                          "rather than leaving a daemon that accepts jobs it "
                          "will never run")
            self._crash()
        finally:
            self._teardown()

    def _crash(self):
        """Fail every live job and stop, so clients get an error, not a hang."""
        self._fail_live("the broker's scheduler thread died; the job never ran "
                        "to completion. Re-submit it.")

    def _fail_live(self, reason: str, *, keep_running: bool = False):
        """Stop, failing every non-terminal job with ``reason``.

        Sets the state directly rather than through _finish: this can run on a
        server thread or while a worker is still inside the session, and
        _finish may restore a seed -- a session call. The restore is left to
        _teardown on the scheduler thread. ``keep_running`` spares the job
        whose chunk is in flight; the scheduler fails it at the boundary.
        """
        try:
            with self.cond:
                self._stop = True
                for j in self._non_terminal():
                    if keep_running and j.state == JobState.RUNNING:
                        continue
                    j.state = JobState.FAILED
                    j.error = reason
                    j.finished = time.time()
                self.cond.notify_all()
        except Exception:
            log.exception("failing live jobs on stop failed")

    def _scheduler_loop(self):
        while not self._stop:
            # Both of these run on the scheduler thread, before anything else
            # touches the session: reaping keeps the single-actor invariant, and
            # restore-on-cancel must not happen on a server thread.
            self._reap_zombie()
            if self._stop:
                # Reaping gave up and stopped the broker: not one more session
                # call, and not the idle path either -- that releases the lease.
                break
            self._restore_if_pending()
            if self._stop:
                # A restore that could not be stopped: same as a wedged worker.
                break
            with self.cond:
                self._reap_old_jobs_locked()
                self._fail_seed_gated_locked()
                job = self._runnable()
                if job is None:
                    # idle: maybe release the sim lease + consider idle-exit
                    self._go_idle_locked()
                    if not self._non_terminal():
                        idle_for = time.time() - self._last_activity
                        wait = max(1.0, IDLE_EXIT_S - idle_for)
                        self.cond.wait(timeout=self._capped_by_restore(wait))
                        if (not self._non_terminal()
                                and time.time() - self._last_activity >= IDLE_EXIT_S):
                            self._stop = True
                    else:
                        self.cond.wait(timeout=self._capped_by_restore(5.0))
                    continue
                # leaselib is None where fcntl is missing: there is no lease to
                # take, and without this guard the loop below spun forever.
                need_lease = self._sim_lease_cm is None and leaselib is not None
            if need_lease:
                # Taken OUTSIDE the lock: the acquire blocks for up to
                # SIM_LEASE_TIMEOUT_S while someone else holds the simulator,
                # and every op handler needs this lock -- so status, cancel and
                # bump all hung for minutes at exactly the moment a user wanted
                # to see who was blocking them, or give up.
                lease_error = self._go_busy()
                with self.cond:
                    if lease_error:
                        # Every live job, not just this one: the broker was idle
                        # (that is when it lets go of the lease), so none of
                        # them has run, and each would otherwise sit through its
                        # own full timeout against the same holder -- ten
                        # queued screens, half an hour of serial failures.
                        for j in self._non_terminal():
                            self._fail(j, lease_error)
                    self.cond.notify_all()
                # Re-select from scratch: the queue may have changed while we
                # waited (this job cancelled, something more urgent arrived).
                continue
            with self.cond:
                if self._stop or self._runnable() is not job:
                    continue
                # Preemption bookkeeping: if we're switching away from a
                # different still-active job, mark it PAUSED.
                if (self._last_job_id and self._last_job_id != job.id
                        and self._last_job_id in self.jobs):
                    prev = self.jobs[self._last_job_id]
                    if prev.state not in TERMINAL and prev.chunk_ix < len(prev.chunks):
                        prev.state = JobState.PAUSED
                        # It is back in the queue, so its aging clock restarts
                        # from now -- being preempted is what aging exists to
                        # compensate for.
                        prev.mark_waiting()
                job.state = JobState.RUNNING
                # Stop the aging clock while it holds the simulator: run time
                # is not queueing delay, and counting it let a long batch job
                # promote itself past the interactive work it was blocking.
                job.mark_running()
                if job.started is None:
                    job.started = time.time()
                    # Once per job: the level cannot change what a job sees
                    # mid-run, and it must stay cheap and never throw.
                    try:
                        v = preflight.pressure_verdict(preflight.memory_pressure())
                    except Exception:
                        v = None
                    if v and v[0] == "fail":
                        self._fail(job, v[1])
                        self.cond.notify_all()
                        continue
                    if v:
                        job.warnings.append(v[1])
                self._last_job_id = job.id
                self._rewind_locked(job)
                chunk = job.chunks[job.chunk_ix]
            # Execute the chunk OUTSIDE the lock (may take seconds).
            self._run_chunk(job, chunk)

    def _in_worker(self, fn, max_s: float):
        """Run one session call on a worker thread, bounded by ``max_s``.

        Returns ``(box, thread)``; ``box`` carries "r" on success or "e"/"tb"
        on failure, and a still-alive thread means the budget was blown. The
        caller decides what a timeout means -- every session call the scheduler
        makes goes through here so that no path can block it forever.

        BaseException, not Exception: the capture layer historically raised
        SystemExit, which must become a job failure, never a silently-dead
        thread mis-read as success.
        """
        box = {}

        def worker():
            try:
                box["r"] = fn()
            except BaseException as e:  # noqa: BLE001 -- see docstring
                box["e"] = e
                box["tb"] = traceback.format_exc()

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(max_s)
        return box, t

    def _run_chunk(self, job: Job, chunk: Chunk):
        box, t = self._in_worker(lambda: self._dispatch(job, chunk), chunk.max_s)
        # One reading of the worker's liveness for every decision below. Read
        # twice, a SEED worker finishing between the reads was both recorded as
        # the live owner and had its restore scheduled -- the next loop round
        # then restored the patch out from under the job that still needs it.
        alive = t.is_alive()
        with self.cond:
            self._last_activity = time.time()
            if chunk.kind in (ChunkKind.LOAD, ChunkKind.TEST_BATCH):
                # Whatever the outcome -- a failed or timed-out push may still
                # have replaced the running app.
                self._load_gen += 1
            if chunk.kind == ChunkKind.SEED and self._seed_job_id is None:
                # Whatever the outcome -- a SEED that timed out, was cancelled
                # in flight or raised part-way may still have patched the tree,
                # and then nobody is recorded as its owner. Remember WHICH
                # tree, so _blocked_by_seed can hold back builds of it (and only
                # it) until the restore has really run. Never overwritten while
                # another job owns a live seed: that is its tree, not ours.
                self._seed_app_dir = _realdir((chunk.args or {}).get("app_dir"))
                if alive or job.state in TERMINAL or "e" in box:
                    # It will not be recorded as the owner below, so nobody's
                    # _finish restores it: schedule the restore here, where the
                    # patch came from. (The gate kept every other SEED out
                    # while this ran, so no live owner's patch is in the way.)
                    self._restore_pending = True
            if alive:
                # Hard timeout. The worker is still inside SimSession calls, so
                # it cannot simply be abandoned: starting the next job's chunk
                # now would put two threads on the GUI at once. Hand it to the
                # scheduler loop, which restarts the sim to unblock it and waits
                # for it to die before running anything else.
                self._zombie = t
                # Its restore, if it owns a seed, is only scheduled (_finish):
                # the loop reaps this worker before it drains the flag.
                self._fail(job, f"chunk '{chunk.kind}' exceeded max_s={chunk.max_s}s")
                self.cond.notify_all()
                return
            if job.state in TERMINAL:
                # Cancelled while this chunk was in flight. The chunk boundary
                # is the only preemption point, so it ran to completion — but
                # its result is discarded and the job stops here, keeping the
                # state the cancel gave it.
                self._finish(job, job.state)
                self.cond.notify_all()
                return
            if "e" in box:
                self._fail(job, f"{chunk.kind}: {box['e']}")
                self.cond.notify_all()
                return
            # Seed ownership is recorded HERE, not in _dispatch: _dispatch runs
            # on the worker thread, and a SEED that only lands after its chunk
            # timed out would otherwise name a job that is already dead as the
            # owner -- an owner whose _finish has been and gone, so nothing
            # would ever restore the patch again. Recorded on this thread, an
            # unowned patch gets its restore scheduled above instead.
            if chunk.kind == ChunkKind.SEED:
                self._seed_job_id = job.id
                self._seed_app_dir = _realdir((chunk.args or {}).get("app_dir"))
            elif chunk.kind == ChunkKind.RESTORE:
                self._clear_seed_locked()
            elif chunk.kind == ChunkKind.LOAD:
                self._job_load_gen[job.id] = self._load_gen
            job.results.append({"kind": chunk.kind, "result": box.get("r")})
            job.chunk_ix += 1
            if chunk.kind == ChunkKind.TEST_BATCH:
                # A test run that did not come back green is a failed job. The
                # chunk "ran", but an unreachable simulator, zero tests or a
                # failing suite reported as state=done / ok=true is a lie.
                res = box.get("r")
                if not isinstance(res, dict) or not res.get("ok"):
                    why = res.get("reason") if isinstance(res, dict) else None
                    self._fail(job, "tests did not pass: "
                               + (why or "failures or no result"))
                    self.cond.notify_all()
                    return
            if job.chunk_ix >= len(job.chunks):
                self._finish(job, JobState.DONE)
            self.cond.notify_all()

    # ── chunk dispatch ────────────────────────────────────────────────────────
    def _dispatch(self, job: Job, chunk: Chunk):
        # Optional arguments are passed only when the chunk carries them, so a
        # default lives in exactly one place -- the SimSession signature -- and
        # cannot drift from a fallback copied in here. Each kind still names
        # what it forwards: a stray key from the wire is dropped, not splatted
        # into a session call. (refocus_every is deliberately local-only.)
        #
        # Runs on the worker thread, so it must not write into the job: a
        # chunk that outlives its budget would keep mutating a job the
        # scheduler has already failed and moved past.
        a = chunk.args or {}
        s = self.session
        k = chunk.kind
        # The chunk's max_s is one budget for everything this chunk calls; see
        # BUDGET_MARGIN_S for why no call may be handed all of it.
        t0 = time.monotonic()

        def left() -> float:
            return max(1.0, chunk.max_s - (time.monotonic() - t0) - BUDGET_MARGIN_S)

        if k == ChunkKind.ENSURE:
            return s.ensure(a.get("project", "."))
        if k == ChunkKind.SEED:
            return s.seed(a["app_dir"], a.get("consts"), a.get("patches"),
                          **_given(a, "force"))
        if k == ChunkKind.RESTORE:
            return s.restore()
        if k == ChunkKind.LOAD:
            prg = a.get("prg")
            built = False
            if not prg and a.get("app_dir"):
                # Built afresh every time this chunk runs, a rewind included.
                # The output path (bin/NAME.prg) is the same for every device,
                # seed and job, so a path remembered from an earlier run names
                # whatever was built there LAST -- possibly another job's
                # device, or its seeded tree -- and pushing it reports that
                # build as this job's verified screen.
                prg = s.build(a["app_dir"], a.get("device", job.device),
                              timeout_s=left())
                built = True
            # A freshly-built prg must be pushed even if the same app is "warm":
            # build.sh writes the SAME output path each time, so warm-skip (keyed
            # on app+device) would otherwise show the previous screen's build.
            return s.load(job.app, prg, a.get("device", job.device),
                          project=a.get("project"),
                          anchor=a.get("anchor"),
                          # A fresh build is always pushed (see above); else
                          # the chunk's own force, else the session default.
                          **({"force": True} if built else _given(a, "force")),
                          **({"storage_start": a["storage"]} if "storage" in a else {}),
                          **_given(a, "fit"))
        if k == ChunkKind.ACT:
            return s.act(**_given(a, "kind", "x", "y", "direction", "key", "confirm",
                                  "anchor", "retries", "hold", "path"),
                         **({"swipe": sw} if a.get("kind") == "swipe"
                            and (sw := proto.swipe_spec(a)) else {}),
                         # `id` canonical, `button` alias: proto.button_id.
                         **({"button": b} if (b := proto.button_id(a)) else {}),
                         **({"state": a.get("state", a.get("value"))}
                            if a.get("kind") == "phone" else {}))
        if k == ChunkKind.SHOT:
            return s.shot(a["name"],
                          **_given(a, "crop", "frame", "anchor", "out_dir",
                                   "settle_s", "marker", "marker_timeout_s",
                                   "marker_regex"))
        if k == ChunkKind.RECORD:
            return s.record(a["name"],
                            **_given(a, "duration_s", "interval_ms", "crop",
                                     "frame", "out_dir", "settle_s",
                                     "keep_frames", "sheet_cols", "max_frames",
                                     "playback_ms"))
        if k == ChunkKind.REACH:
            return s.reach(a.get("steps", []), **_given(a, "anchor", "screen"))
        if k == ChunkKind.TEST_BATCH:
            prg = a.get("prg")
            if not prg and a.get("app_dir"):
                # Same place LOAD builds: inside the session, on the scheduler
                # thread. tests=True selects the `monkeyc -t` variant.
                prg = s.build(a["app_dir"], a.get("device", job.device),
                              timeout_s=left(), tests=True)
            # What the build left over, not max_s afresh: handing both calls
            # the whole budget let build + tests run up to twice max_s, so any
            # run needing more than max_s in total hit the zombie path and a
            # simulator restart instead of failing as a test timeout. run_tests
            # spends it as one deadline (ensure, port wait, retry included).
            return s.run_tests(prg, a.get("device", job.device),
                               timeout_s=left())
        raise SimError(f"unknown chunk kind {k}")

    # ── resume: rewind to the job's own LOAD ──────────────────────────────────
    def _reload_ix(self, job: Job) -> int | None:
        """Index of this job's last LOAD if its app is no longer up, else None.

        The "is it already loaded?" test is keyed on the LOAD chunk's own
        project path, not on ``job.app`` -- which is only the project
        directory's BASENAME, and therefore identical across two git worktrees
        of the same repo. Comparing basenames let a job decide someone else's
        checkout was its own app and skip the reload, then screenshot the
        other build and report it verified.

        Even the full path is not enough on its own: it names the checkout, not
        the BUILD. Two jobs on one project (one seeded, one not; or a pre-built
        .prg against a fresh build) share the key, so after the other one
        pushed, "the right app is loaded" was true and this job shot the other
        job's build.

        Hence the load generation, and nothing else: every push bumps it, so
        "unchanged since my own LOAD, and the simulator not restarted since"
        means the app up is the one this job pushed. No key is recomputed
        here. Comparing one against the key the session stored made the two
        derivations a contract -- and they disagreed (a chunk with ``app_dir``
        but no ``project``, a build that writes outside ``bin/``), so the job
        found its own fresh LOAD "not loaded" and rewound to it forever.
        """
        for i in range(min(job.chunk_ix, len(job.chunks)) - 1, -1, -1):
            if job.chunks[i].kind != ChunkKind.LOAD:
                continue
            if (self._job_load_gen.get(job.id) == self._load_gen
                    and self.session.current is not None):
                return None
            return i
        return None   # job never loaded an app yet; nothing to restore

    def _rewind_ix(self, job: Job) -> int:
        """Index of the chunk ``job`` will actually run next."""
        if job.chunks[job.chunk_ix].kind in SCREEN_KINDS:
            ix = self._reload_ix(job)
            if ix is not None:
                return ix
        return job.chunk_ix

    def _rewind_locked(self, job: Job) -> None:
        """Before a screen-touching chunk, rewind to the job's LOAD if another
        job pushed since -- and re-run everything from there.

        Re-pushing the app alone is not a resume. It starts on its first
        screen with fresh storage, so a job preempted between REACH and SHOT
        came back, skipped the navigation it had already "done", and saved the
        start screen under the target screen's name. Rewinding replays LOAD
        and every gesture after it as ordinary chunks: each with its own
        budget, its own preemption point and the one settlement path in
        _run_chunk -- and it is the LOAD, not a hidden pre-step, that
        _blocked_by_seed sees and holds back while another job's seed is live.

        Caller holds the lock; the scheduler thread is the only writer of
        chunk_ix, so this cannot race a chunk in flight.
        """
        ix = self._rewind_ix(job)
        if ix == job.chunk_ix:
            return
        log.info("job %s: its app was replaced while it was paused; rewinding "
                 "from chunk %d to its LOAD (chunk %d)", job.id, job.chunk_ix, ix)
        load = job.chunks[ix]
        # Forced: the session's own warm-skip keys on the same checkout path,
        # so it would decline to push over another job's build of this project
        # -- the very case that brought us here.
        job.chunks[ix] = dataclasses.replace(
            load, args={**(load.args or {}), "force": True})
        job.chunk_ix = ix
        # One result per finished chunk; the rewound ones will report again.
        del job.results[ix:]

    # ── terminal transitions ──────────────────────────────────────────────────
    def _fail(self, job: Job, reason: str):
        job.error = reason
        self._finish(job, JobState.FAILED)

    def _finish(self, job: Job, state: str):
        """Caller holds the lock. Never touches the session."""
        # A job that is already terminal keeps the state it was given. Without
        # this, cancelling a job whose last chunk is in flight reports back as
        # DONE — a cancel the user watched succeed, contradicted by the result.
        if job.state not in TERMINAL:
            job.state = state
            job.finished = time.time()
        # Safety net: never leave a SEED_* patch behind. Only the job that
        # applied the patch may undo it — restoring here unconditionally would
        # rip an active seed out from under a *different* job that is merely
        # paused between chunks, and its next build would come out unseeded.
        # An unowned patch (_seed_job_id None) is not this job's either: its
        # restore was scheduled where it was applied (_run_chunk), and with
        # the owner-less gate every job it holds back is failed through here --
        # restoring per failed job ran them back to back, ignoring the backoff.
        #
        # Scheduled, never run inline: the caller holds the lock, and a
        # restore under it (up to ZOMBIE_GRACE_S) blocked every STATUS, CANCEL
        # and WAIT past the client's timeout. _restore_if_pending runs it at
        # the top of the next loop round, outside the lock, after reaping a
        # timed-out worker that may still be inside the session; meanwhile the
        # raised flag keeps the gate up.
        if self._seed_job_id is not None and self._seed_job_id == job.id:
            self._restore_pending = True
        if self._last_job_id == job.id:
            self._last_job_id = None
        self._job_load_gen.pop(job.id, None)

    def _restore_now(self):
        """Undo the active seed. Scheduler thread only, WITHOUT the lock (via
        _restore_if_pending): a restore can take up to ZOMBIE_GRACE_S, and every
        op handler needs the lock -- held across it, STATUS, CANCEL and WAIT
        outwaited the client's cheap-RPC timeout. Releasing it is safe because
        only this thread runs chunks or touches the session, and
        _restore_pending stays raised until the restore has succeeded, so the
        seed gate is up throughout. Idempotent.

        Bounded like every other session call the scheduler makes: a restore
        is file writes plus a `git diff`, but on a hung network share or a
        wedged git it would otherwise block the scheduler thread -- the broker
        then accepts jobs it never runs. A restore still running after
        ZOMBIE_GRACE_S stops the broker, as a wedged chunk worker does: the
        worker is inside the session, so nothing else may be started beside it.
        """
        if self._restore_wedged is not None:
            # One is still stuck inside the session and the broker is stopping;
            # a second restore beside it could interleave their file writes.
            return
        box, t = self._in_worker(self.session.restore, ZOMBIE_GRACE_S)
        if t.is_alive():
            log.error("restoring a seeded file is still running after %.0fs; "
                      "stopping the broker rather than run beside it",
                      ZOMBIE_GRACE_S)
            self._restore_wedged = t
            with self.cond:
                self._restore_pending = True    # the gate stays up until exit
            self._fail_live(
                f"restoring a seeded file did not finish within "
                f"{ZOMBIE_GRACE_S:.0f}s, so the broker stopped rather than run "
                f"another job beside it. Run `ciqkit sim restore` (it replays "
                f"the seed journal), then re-submit.")
            return
        if "e" in box:
            e = box["e"]
            if not isinstance(e, Exception):
                raise e     # SystemExit & co. stay fatal, as on this thread before
            # The patch is still in the tree (the session keeps its backup), so
            # the seed gate must stay up: clearing it here let the next job
            # build the seeded source and report the result as verified.
            # Re-raising the flag is also the retry -- the scheduler loop
            # drains it again, after a backoff, while the owner is terminal
            # and no _finish of the owner will come round again.
            # Full traceback once; the retries would fill brokerd.log with
            # the same one.
            first = self._restore_failures == 0
            if first:
                log.error("restoring a seeded file failed; will retry\n%s",
                          box.get("tb", ""))
            else:
                log.warning("restoring a seeded file failed again: %s", e)
            with self.cond:
                now = time.monotonic()
                if self._restore_failing_since is None:
                    self._restore_failing_since = now
                self._restore_failures += 1
                self._restore_error = f"{type(e).__name__}: {e}"
                self._restore_pending = True
                # Exponential backoff: back-to-back retries cannot outlast a
                # transient cause, they only use up the give-up window.
                delay = min(RESTORE_RETRY_MAX_S,
                            RESTORE_RETRY_MIN_S * 2 ** min(self._restore_failures - 1, 16))
                self._restore_next_at = now + delay
            return
        with self.cond:
            self._clear_seed_locked()
            self.cond.notify_all()

    def _clear_seed_locked(self):
        """The patch is out of the tree: drop the seed and everything that
        tracks its restore, together. Clearing the seed alone left
        _restore_pending raised with no tree named, which gates EVERY build --
        and a failure clock past RESTORE_GIVE_UP_S then failed those jobs as
        "could not be restored after 0 attempts". Caller holds the lock."""
        self._seed_job_id = None
        self._seed_app_dir = None
        self._restore_pending = False
        self._restore_failures = 0
        self._restore_error = ""
        self._restore_failing_since = None
        self._restore_next_at = 0.0

    def _restore_gave_up(self) -> bool:
        """Has the restore been failing for longer than RESTORE_GIVE_UP_S?"""
        return (self._restore_failing_since is not None
                and time.monotonic() - self._restore_failing_since >= RESTORE_GIVE_UP_S)

    def _fail_seed_gated_locked(self):
        """Fail the jobs a restore that keeps failing holds back.

        The gate must stay up (the patch is still in the tree), but queueing
        behind it for ever left every seeding or building job QUEUED with no
        word on why. Once restores have been failing for RESTORE_GIVE_UP_S they
        fail with the restore error; the retries go on, and a later job is
        judged afresh. Caller holds the lock.
        """
        if not self._restore_pending or not self._restore_gave_up():
            return
        failed = False
        for j in list(self.jobs.values()):
            if (j.state not in TERMINAL and j.chunk_ix < len(j.chunks)
                    and self._blocked_by_seed(j)):
                self._fail(j, f"a seeded file could not be restored after "
                              f"{self._restore_failures} attempts over "
                              f"{RESTORE_GIVE_UP_S:.0f}s "
                              f"({self._restore_error}); fix the file (the broker keeps "
                              "retrying), or run `ciqkit sim shutdown` and then "
                              "`ciqkit sim restore`, which replays the seed journal")
                failed = True
        if failed:
            self.cond.notify_all()

    def _restore_retry_in(self) -> float | None:
        """Seconds until a pending restore is due again; None if none is
        pending. Caller holds the lock. The idle wait is capped by it so a
        backed-off retry runs on time instead of at the next 5 s tick."""
        if not self._restore_pending:
            return None
        return max(0.0, self._restore_next_at - time.monotonic())

    def _capped_by_restore(self, wait: float) -> float:
        """``wait``, shortened so a backed-off restore retry is not late.
        Caller holds the lock."""
        due = self._restore_retry_in()
        return wait if due is None else min(wait, max(0.05, due))

    def _restore_if_pending(self):
        """Honour a restore a CANCEL asked for, or retry one that failed.

        The CANCEL handler runs on a server thread, which must never touch the
        session, so it only raises the flag; the actual restore happens here.
        Without it a job cancelled after its SEED chunk leaves the user's source
        patched until some *other* job happens to finish. A retry waits out its
        backoff: this returns at once until then, so the loop keeps scheduling
        work the stuck seed does not gate. The flag is left raised while the
        restore runs: _restore_now clears it with the seed on success, and a
        failure keeps it.
        """
        with self.cond:
            if not self._restore_pending:
                return
            if time.monotonic() < self._restore_next_at:
                return
        self._restore_now()

    def _reap_zombie(self):
        """Wait for a timed-out chunk's worker thread to actually stop.

        A chunk that blew through max_s left its worker running inside
        SimSession calls. Running the next chunk while it is alive breaks the
        single-actor invariant the whole design rests on -- and it happens
        exactly when the simulator is already misbehaving, which is the worst
        moment to add a second driver. Restarting the sim is what unblocks a
        worker wedged in a GUI or subprocess call, so do that, then wait for it,
        bounded.
        """
        t = self._zombie
        if t is None:
            return
        self._zombie = None
        if not t.is_alive():
            return
        log.warning("a chunk timed out and its worker is still running; "
                    "restarting the simulator to unblock it")
        # On a worker of its own, bounded like every other session call: a
        # restart that hangs (a simulator that will not die, a stuck relaunch)
        # used to block this thread for good -- a broker that accepts jobs and
        # never runs them. A restart still running after the grace is a second
        # wedged thread in the session, so it ends the broker like the first.
        rbox, rt = self._in_worker(self.session.restart, ZOMBIE_GRACE_S)
        if rt.is_alive():
            log.error("restarting the simulator to free a timed-out worker is "
                      "itself still running after %.0fs; stopping the broker",
                      ZOMBIE_GRACE_S)
            self._fail_live(
                f"a chunk timed out and the simulator restart meant to free it "
                f"did not finish within {ZOMBIE_GRACE_S:.0f}s, so the broker "
                f"stopped rather than drive the simulator from two threads. "
                f"Re-submit; a fresh broker starts on demand.")
            return
        if "e" in rbox:
            if not isinstance(rbox["e"], Exception):
                # SystemExit & co. stay fatal, as when this ran on this thread:
                # scheduler() turns them into "fail everything and stop".
                raise rbox["e"]
            log.error("restart while reaping a timed-out worker failed\n%s",
                      rbox.get("tb", ""))
        deadline = time.time() + ZOMBIE_GRACE_S
        while t.is_alive() and time.time() < deadline:
            t.join(1.0)
            with self.cond:
                # Reaping is work, not idleness -- don't let idle-exit fire.
                self._last_activity = time.time()
        if t.is_alive():
            # Nothing left to try: after a sim restart its handles are dead, so
            # the thread is stuck in something we cannot interrupt -- and Python
            # cannot kill a thread. This process can no longer promise a single
            # actor, so it must not run another chunk (nor release the `sim`
            # lease to a script while the worker may still be on the GUI).
            # Stop instead: process exit is what finally kills the worker, and
            # the next client command spawns a clean broker.
            log.error("a timed-out chunk's worker is STILL running after %.0fs; "
                      "stopping the broker rather than put a second driver on "
                      "the simulator", ZOMBIE_GRACE_S)
            self._fail_live(
                f"a timed-out chunk's worker could not be stopped within "
                f"{ZOMBIE_GRACE_S:.0f}s even after a simulator restart, so the "
                f"broker stopped rather than drive the simulator from two "
                f"threads. Re-submit; a fresh broker starts on demand.")

    def _reap_old_jobs_locked(self):
        """Drop long-finished jobs so the table does not grow without bound.

        The job table is in memory on purpose (no persistence), so a reaped job
        is simply gone -- which is fine, every command here is cheap to
        re-issue. The window is far longer than any client wait timeout, so a
        client waiting on a job cannot lose it out from under itself.
        """
        cutoff = time.time() - JOB_RETENTION_S
        stale = [jid for jid, j in self.jobs.items()
                 if j.state in TERMINAL and (j.finished or 0) < cutoff]
        for jid in stale:
            del self.jobs[jid]
            # A job cancelled while PAUSED never passes through _finish, so its
            # load generation would otherwise outlive the job forever.
            self._job_load_gen.pop(jid, None)

    # ── sim-lease busy/idle ────────────────────────────────────────────────────
    def _go_busy(self) -> str | None:
        """Take the machine-wide `sim` lease. Returns None on success, else why not.

        Scheduler thread only, and WITHOUT the broker lock held -- see the call
        site. Only the scheduler thread (and teardown) touches _sim_lease_cm.

        Failure is NOT survivable, which is a change: it used to log a warning
        and drive the simulator anyway. That quietly voided the contract the
        lease exists to provide -- "scripts that drive the simulator without
        going through the broker still serialise against it" -- at exactly the
        moment it mattered, because a timeout means someone else is holding it
        and is on the GUI right now. Two actors on one simulator is the single
        thing this design forbids, so a job that cannot take the lease fails
        and names the holder instead of racing them.

        The acquire already blocks for SIM_LEASE_TIMEOUT_S, so this is a
        bounded wait, not an instant refusal.
        """
        if self._sim_lease_cm is not None or leaselib is None:
            return None
        try:
            # should_stop: a SHUTDOWN arriving mid-wait must not have to sit
            # out the rest of the timeout.
            cm = leaselib.lease("sim", timeout=SIM_LEASE_TIMEOUT_S,
                                meta={"who": "ciqsimd"},
                                should_stop=lambda: self._stop)
            cm.__enter__()
            self._sim_lease_cm = cm
            return None
        except Exception as e:  # noqa: BLE001 -- LeaseTimeout and friends
            self._sim_lease_cm = None
            holder = ""
            try:
                # status() is best-effort sidecar data: a list of every held
                # lease, so pick ours out of it.
                for h in leaselib.status():
                    if h.get("resource") == "sim" and h.get("alive"):
                        holder = (f" Held by pid {h.get('pid')}"
                                  f"{' (' + str(h['who']) + ')' if h.get('who') else ''}"
                                  f".")
                        break
            except Exception:
                pass
            log.warning("could not take the 'sim' lease: %s", e)
            return (f"could not take the machine-wide 'sim' lease within "
                    f"{SIM_LEASE_TIMEOUT_S:.0f}s, so something else is driving "
                    f"the simulator.{holder} Refusing to drive it at the same "
                    f"time -- wait for the other run to finish, or stop it. "
                    f"({e})")

    def _go_idle_locked(self):
        if self._sim_lease_cm is None:
            return
        try:
            self._sim_lease_cm.__exit__(None, None, None)
        except Exception:
            pass
        self._sim_lease_cm = None

    def _teardown(self):
        # Restore any active SEED_* patch before we go away, so a shutdown or a
        # SIGTERM (e.g. pkill) never leaves SEED_*=true / a stray source patch in
        # the tree. SIGKILL can't be caught — that still needs a manual revert.
        # Bounded, so a hung restore cannot keep a stopping daemon alive; and
        # skipped beside a restore already wedged in the session -- the seed
        # journal still holds the original for `ciqkit sim restore`.
        wedged = self._restore_wedged
        failed = ""
        if wedged is not None and wedged.is_alive():
            failed = "an earlier restore is still stuck"
        else:
            box, t = self._in_worker(self.session.restore, ZOMBIE_GRACE_S)
            if t.is_alive() or "e" in box:
                failed = "timed out" if t.is_alive() else f"failed: {box['e']}"
        if failed:
            log.error("restoring seeds at exit: %s; run `ciqkit sim restore` "
                      "to replay the seed journal", failed)
        with self.cond:
            if failed:
                # The patch may still be in the source: keep the seed state, so
                # a WAIT on its owner never reads ok=True, and say why.
                self._restore_abandoned = (
                    f"restoring it at broker exit {failed}; run `ciqkit sim restore`, "
                    "which replays the seed journal")
            else:
                self._clear_seed_locked()
            self._go_idle_locked()
            self.cond.notify_all()      # waiters re-judge now, not at their next poll

    # ── op handlers (called from server threads) ───────────────────────────────
    def handle(self, req: dict) -> dict:
        # Malformed requests answer "bad request: <what>" rather than the text
        # of whatever exception the missing key raised further down ("'list'
        # object has no attribute 'get'", "'job_id'"), which named no field.
        if not isinstance(req, dict):
            return _bad("the request must be a JSON object")
        op = req.get("op")
        if op in (Op.WAIT, Op.BUMP, Op.CANCEL) and not isinstance(req.get("job_id"), str):
            return _bad(f"{op} needs a string job_id")
        if op == Op.PING:
            # install/version let a client notice it is talking to a broker
            # started from another checkout (see `ciqkit doctor`).
            import ciqkit
            return {"ok": True, "pid": os.getpid(),
                    "uptime_s": time.time() - _START,
                    "version": ciqkit.__version__,
                    "install": os.path.dirname(os.path.abspath(ciqkit.__file__)),
                    "as_app": os.environ.get("CIQKIT_APP") == "1"}
        if op == Op.PREFLIGHT:
            # Answered here and not by the caller: macOS decides per responsible
            # process, and that is the broker's, not the asking terminal's.
            from . import preflight
            return preflight.report(request=req.get("request") is True)
        if op == Op.SUBMIT:
            if not isinstance(req.get("job"), dict):
                return _bad("submit needs a job object")
            job = Job.from_dict(req["job"])
            errs = proto.validate_job(job)
            if errs:
                return proto.error_reply(proto.E_INVALID,
                                         "invalid job: " + "; ".join(errs))
            with self.cond:
                if self._stop:
                    # Told to stop, but the accept loop can still take
                    # connections for up to a second. A job accepted now would
                    # be dropped with the job table, and its WAIT would land on
                    # the next broker as "no such job". Refuse it so the client
                    # can resubmit there. Checked under the lock: idle-exit sets
                    # _stop under it, and a check before taking it let a submit
                    # slip into a broker that had already decided to exit.
                    return proto.error_reply(proto.E_SHUTTING_DOWN,
                                             proto.ERR_SHUTTING_DOWN)
                if job.id in self.jobs:
                    # The id is client-chosen. Accepting a duplicate replaced
                    # the live entry -- possibly a RUNNING job -- in the table.
                    return proto.error_reply(
                        proto.E_INVALID, f"a job with id {job.id} already exists")
                # The aging clock starts when WE accept the job. Job.from_dict
                # already refuses to read it from the wire; this is the second
                # half of the same rule, stated where the clock actually starts.
                job.waiting_since = None
                job.waited_total = 0.0
                job.mark_waiting()
                self.jobs[job.id] = job
                self._last_activity = time.time()
                self.cond.notify_all()
            return {"ok": True, "job_id": job.id}
        if op == Op.WAIT:
            timeout_s = req.get("timeout_s", 600)
            # bool is an int subclass; True is not a timeout anyone meant.
            if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                    or not math.isfinite(timeout_s)):
                return _bad("wait timeout_s must be a finite number")
            return self._wait(req["job_id"], float(timeout_s))
        if op == Op.STATUS:
            with self.lock:
                jobs = [self._job_brief(j) for j in self.jobs.values()]
                cur = self._last_job_id
            # Read once: the scheduler thread reassigns session.current without
            # our lock (a restart sets it to None), so testing it and then
            # reading it again could land on None between the two and turn a
            # status call into "'NoneType' object is not iterable".
            loaded = self.session.current
            return {"ok": True, "jobs": jobs, "current": cur,
                    "loaded": list(loaded) if loaded else None}
        if op == Op.BUMP:
            with self.cond:
                j = self.jobs.get(req["job_id"])
                if not j:
                    return proto.error_reply(proto.E_NO_SUCH_JOB, "no such job")
                prio = req.get("priority", int(proto.Priority.URGENT))
                if (e := proto.validate_priority(prio)) is not None:
                    return proto.error_reply(proto.E_INVALID, e)
                j.priority = prio
                self.cond.notify_all()
            return {"ok": True, "priority": j.priority}
        if op == Op.CANCEL:
            with self.cond:
                j = self.jobs.get(req["job_id"])
                if not j:
                    return proto.error_reply(proto.E_NO_SUCH_JOB, "no such job")
                if j.state not in TERMINAL:
                    j.state = JobState.CANCELLED
                    j.finished = time.time()
                    # A chunk already in flight still runs to completion -- the
                    # chunk boundary is the only preemption point -- but no
                    # further chunk of this job will start. If it had seeded,
                    # ask the scheduler thread to put the source back; this
                    # thread must not touch the session itself.
                    if self._seed_job_id == j.id:
                        self._restore_pending = True
                self.cond.notify_all()
            return {"ok": True}
        if op == Op.SHUTDOWN:
            # Queued and paused jobs will never run in this process: fail them
            # now, so their WAITs answer with a reason instead of a dropped
            # socket. The chunk in flight finishes (it is bounded by max_s and
            # the chunk boundary is the only safe stopping point); serve() then
            # waits for the scheduler before the process exits.
            self._fail_live(f"{proto.ERR_SHUTTING_DOWN}: the job was stopped "
                            f"before it ran to completion; re-submit it",
                            keep_running=True)
            return {"ok": True}
        return proto.error_reply(proto.E_BAD_REQUEST, f"unknown op {op}")

    def _wait(self, job_id: str, timeout_s: float) -> dict:
        deadline = time.time() + timeout_s
        with self.cond:
            while True:
                j = self.jobs.get(job_id)
                if j is None:
                    return proto.error_reply(proto.E_NO_SUCH_JOB, "no such job")
                if j.state in TERMINAL:
                    owns_patch = self._seed_job_id == j.id and self._restore_pending
                    if not (owns_patch and not self._restore_gave_up()
                            and self._restore_wedged is None
                            and not self._restore_abandoned):
                        # A seed owner's restore runs at the top of the next
                        # loop round, not in _finish (outside the lock). Its
                        # waiters are answered once the patch is out of the
                        # source -- or with ok=False and why, once restoring
                        # it has been failing past RESTORE_GIVE_UP_S: "done"
                        # with the user's source still patched is a result
                        # nobody should act on unverified.
                        reply = {"ok": j.state == JobState.DONE and not owns_patch,
                                 "job": self._job_full(j)}
                        if owns_patch:
                            reply["error"] = (
                                "this job's seed patch is still in the source: "
                                f"{self._restore_abandoned or self._restore_error or 'restore pending'}")
                        return reply
                remaining = deadline - time.time()
                if remaining <= 0:
                    return proto.error_reply(proto.E_WAIT_TIMEOUT, "wait timeout",
                                             job=self._job_full(j))
                self.cond.wait(timeout=min(remaining, 2.0))

    def _job_brief(self, j: Job) -> dict:
        now = time.time()
        return {"id": j.id, "app": j.app, "label": j.label, "state": j.state,
                "priority": j.priority, "eff": round(j.effective_priority(now), 1),
                "chunk": j.chunk_ix, "chunks": len(j.chunks),
                "eta_s": round(j.expected_total_s(), 1), "owner": j.owner}

    def _job_full(self, j: Job) -> dict:
        d = self._job_brief(j)
        d.update({"results": j.results, "error": j.error, "warnings": j.warnings,
                  "started": j.started, "finished": j.finished})
        return d


_START = time.time()


def _given(args: dict, *names: str) -> dict:
    """The subset of ``names`` actually present in a chunk's ``args``."""
    return {n: args[n] for n in names if n in args}


def _realdir(path: str | None) -> str | None:
    return os.path.realpath(os.path.abspath(path)) if path else None


# ── control-channel server ────────────────────────────────────────────────────
def _check_private_dir(sock_path: str) -> None:
    """Refuse a runtime directory other local users could reach."""
    parent = os.path.dirname(os.path.abspath(sock_path))
    if sys.platform == "win32":
        from .. import paths
        try:
            paths._ensure_private(parent)
        except paths.InsecureRuntimeDir as exc:
            raise RuntimeError(
                f"refusing to listen in {parent}: {exc} The control channel can "
                f"start builds, so it must not be reachable by other local "
                f"users.") from exc
        return
    st = os.stat(parent)
    if st.st_uid != os.getuid() or (st.st_mode & 0o077):
        raise RuntimeError(
            f"refusing to listen in {parent}: it must be a directory you own "
            f"with mode 0700 (it is currently {oct(st.st_mode & 0o777)}, "
            f"owned by uid {st.st_uid}). The control socket can start builds, "
            f"so it must not be reachable by other local users.")


def _bind_private(sock_path: str) -> transport.Listener:
    """Bind the control channel so only this user can use it.

    Unix socket, two layers, because neither alone is portable:
      * the socket file itself is created under a 0177 umask and then chmod'ed
        to 0600 -- macOS and most BSDs enforce unix-socket permissions on
        connect;
      * the containing directory is 0700 and ownership-checked (ciqkit.paths),
        which is what actually protects the socket on Linux, where the socket
        file's own mode is historically ignored by ``connect()``.

    Loopback TCP (Windows): the port is open to every local account, so the
    protection is the per-daemon token, readable only through the endpoint
    file in a directory restricted to this user. That directory is checked
    the same way -- if it is not private, the token is not a secret.
    """
    _check_private_dir(sock_path)
    return transport.bind(sock_path)


def _configure_logging():
    """Send the library's diagnostics to the daemon log.

    The capture code logs why it chose a face circle, when it fell back, and
    what it could not find. That is exactly what you need when a screenshot
    comes out wrong, and the daemon log is the right place for it -- it must
    never reach stdout, which carries the CLI's JSON. Set CIQKIT_LOG_LEVEL=DEBUG
    for the detector's per-frame reasoning.
    """
    level = os.environ.get("CIQKIT_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _acquire_singleton_lock(sock_path: str):
    """Take the daemon-singleton lock, or return None if another one holds it.

    Replaces a probe-then-unlink dance -- connect to the socket, and unlink it
    if the connect failed -- which had a real race: a daemon that had bound but
    not yet called listen() refuses connections, so a second daemon starting in
    that window concluded the socket was stale, UNLINKED A LIVE ONE, and bound
    its own. Two brokers, two SimSessions, two actors on one simulator: the one
    thing the whole design forbids.

    An OS file lock (flock / a Windows byte-range lock) has no such window and
    is released by the kernel when the holder dies, so there is nothing stale
    to reap. Holding the fd for the process lifetime IS the lock -- it is
    returned so the caller keeps it alive.
    """
    lock_path = sock_path + ".lock"
    fd = open_lock_file(lock_path)
    try:
        held = try_lock(fd)
    except OSError:
        held = False
    if not held:
        os.close(fd)
        return None
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def serve(sock_path: str | None = None):
    _configure_logging()
    # Belt-and-braces for spawn_daemon stripping it: a daemon started any other
    # way (by hand, from a script under `ciqkit lease sim`) must not inherit a
    # lease it does not hold, or `_go_busy` would never take the real one.
    if leaselib is not None:
        os.environ.pop(leaselib.ENV_KEY, None)
    sock_path = sock_path or proto.socket_path()
    # First, before anything is created, unlinked or restored in that
    # directory: _bind_private checks it again, but only after the lock file,
    # the stale-socket unlink and seed recovery had already happened in it.
    _check_private_dir(sock_path)

    lock_fd = _acquire_singleton_lock(sock_path)
    if lock_fd is None:
        print("[ciqsimd] another daemon is already listening; exiting",
              file=sys.stderr)
        return
    # We hold the singleton lock, so any socket (or endpoint) file still here
    # belongs to a daemon that is gone. Now -- and only now -- it is safe to
    # unlink.
    if os.path.exists(sock_path):
        os.unlink(sock_path)

    # A previous broker that was SIGKILLed mid-seed could not run its own
    # restore. Its journal outlived it, so put those files back before we do
    # anything else — and say so loudly, because the user's tree was modified.
    for path in seedjournal.recover():
        print(f"[ciqsimd] restored {path} — a seed from a previous run had "
              f"left it patched", file=sys.stderr)

    broker = Broker()

    # Restore any active seed on shutdown / SIGTERM / SIGINT so a `pkill ciqsim`
    # can't leave SEED_*=true in the tree. (SIGKILL is uncatchable.)
    import atexit
    import signal
    atexit.register(broker._teardown)

    def _sig(_signum, _frame):
        # First signal: the same orderly stop as SHUTDOWN. Tearing down right
        # here restored seeds from the main thread while a worker could still
        # be inside the session, and dropped every client's WAIT unanswered.
        # A second signal means "now": restore and exit on the spot.
        if not broker._stop:
            broker._fail_live(f"{proto.ERR_SHUTTING_DOWN}: the broker was "
                              f"signalled to stop; re-submit the job",
                              keep_running=True)
            return
        try:
            broker._teardown()
        finally:
            os._exit(0)
    # SIGBREAK: what Windows delivers for Ctrl-Break and a console close,
    # where a POSIX daemon would get SIGTERM/SIGHUP.
    for _s in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGBREAK", None)):
        if _s is None:
            continue
        try:
            signal.signal(_s, _sig)
        except Exception:
            pass

    sched = threading.Thread(target=broker.scheduler, daemon=True)
    sched.start()

    listener = _bind_private(sock_path)
    srv = listener.sock
    srv.settimeout(1.0)
    print(f"[ciqsimd] listening on {sock_path} (pid {os.getpid()})", file=sys.stderr)

    try:
        while not broker._stop:
            if not sched.is_alive():
                # Belt-and-braces: the scheduler's own handler already sets
                # _stop, but a daemon whose scheduler is gone must never keep
                # accepting jobs. Better a connection refused than a job that
                # is accepted and silently never runs.
                print("[ciqsimd] the scheduler thread is gone; stopping",
                      file=sys.stderr)
                broker._crash()
                break
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            # Bounded: a connection thread lives as long as its client keeps
            # the socket open (up to an hour), so one client in a reconnect
            # loop could otherwise pile up threads until the process died.
            # Turning the excess away with an error beats falling over.
            if not _CONN_SLOTS.acquire(blocking=False):
                log.warning("refusing a connection: all %d handler slots are "
                            "busy", MAX_CONNS)
                _refuse(conn, proto.error_reply(
                    proto.E_BUSY, "broker is out of connection slots; retry shortly"))
                continue
            threading.Thread(target=_handle_conn, args=(broker, conn, listener),
                             daemon=True).start()
    finally:
        broker._stop = True
        with broker.cond:
            broker.cond.notify_all()
        try:
            listener.close()
        except OSError:
            pass
        # The scheduler is a daemon thread: returning now would kill it mid-
        # chunk and leave atexit's _teardown restoring seeds on THIS thread
        # while a worker may still be inside the session. Let it reach its
        # chunk boundary, fail what is left and tear down on its own thread.
        sched.join(SHUTDOWN_GRACE_S)
        if sched.is_alive():
            log.warning("the chunk in flight did not finish within %.0fs of "
                        "stopping; exiting anyway", SHUTDOWN_GRACE_S)
            broker._fail_live("the broker shut down while this job's chunk was "
                              "still running; re-submit it")
        # Give WAITs that just saw their job end a moment to send the answer;
        # connection threads are daemons and die with the process.
        deadline = time.time() + 2.0
        while broker._requests_in_flight and time.time() < deadline:
            time.sleep(0.02)
        print("[ciqsimd] stopped", file=sys.stderr)


def _refuse(conn: socket.socket, resp: dict) -> None:
    """Answer and close in a way the client can actually READ.

    Closing a TCP socket that still has unread bytes from the peer sends a
    RST, not a FIN -- Windows does it every time -- and the client, which has
    already written its auth line and request, gets "connection aborted"
    instead of the reason. So: answer, half-close, and briefly drain what the
    client already sent before closing. Bounded, since this runs on the accept
    loop.
    """
    try:
        conn.sendall(proto.dumps(resp))
        conn.shutdown(socket.SHUT_WR)
        conn.settimeout(0.2)
        deadline = time.time() + 0.5
        while time.time() < deadline and conn.recv(4096):
            pass
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _handle_conn(broker: Broker, conn: socket.socket,
                 listener: transport.Listener | None = None):
    """Serve one client connection: JSON lines in, JSON lines out."""
    conn.settimeout(3600)
    try:
        with conn:
            f = conn.makefile("rb")
            if listener is not None:
                # Nothing a connection sends is looked at until it has proved
                # it may talk to us. Short deadline: an unauthenticated peer
                # must not hold a handler slot for an hour.
                conn.settimeout(10)
                if not listener.authenticate(f):
                    # Logs how it failed: a peer that connected and sent
                    # nothing (a port probe, a client that gave up) is not
                    # the same as one holding a wrong token.
                    log.warning("refused a connection that did not authenticate (%s)",
                                "no auth line" if listener.last_auth_empty else "bad token")
                    f.close()
                    _refuse(conn, proto.error_reply(proto.E_UNAUTHORIZED, "unauthorized"))
                    return
                conn.settimeout(3600)
            while True:
                # Bounded read: `for line in f` buffers until a newline, however
                # far away, so one peer could make this thread hold any amount
                # of memory. A line longer than MAX_LINE is refused and the
                # connection dropped -- its tail is not a request boundary we
                # could resynchronise on.
                line = f.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE:
                    log.warning("refused a request line over %d bytes", MAX_LINE)
                    f.close()
                    _refuse(conn, _bad(f"request line exceeds {MAX_LINE} bytes"))
                    return
                if not line.strip():
                    continue
                with broker.cond:
                    broker._requests_in_flight += 1
                try:
                    try:
                        req = proto.loads(line)
                    except ValueError as e:     # not JSON, or not UTF-8
                        resp = proto.error_reply(proto.E_BAD_REQUEST, str(e))
                    else:
                        try:
                            resp = broker.handle(req)
                        except Exception as e:  # noqa: BLE001
                            log.exception("request handler failed")
                            resp = proto.error_reply(proto.E_INTERNAL, str(e))
                    conn.sendall(proto.dumps(resp))
                finally:
                    with broker.cond:
                        broker._requests_in_flight -= 1
    except OSError:
        # The client hung up or the socket broke. Routine, not worth a stack
        # trace -- but it used to be `except Exception: pass`, which also
        # swallowed real bugs in this function and made a daemon that answered
        # nothing look like a daemon with nothing to say.
        log.debug("client connection closed", exc_info=True)
    except Exception:
        log.exception("a connection handler failed")
    finally:
        _CONN_SLOTS.release()


if __name__ == "__main__":
    serve()
