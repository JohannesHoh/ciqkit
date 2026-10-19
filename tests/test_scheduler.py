"""Scheduler + preemption unit tests with a mock SimSession.

Proves the broker core (priority ordering, chunk-boundary preemption, resume +
app reload, bump) independently of the real simulator. No GUI, no SDK, no
Connect IQ install required -- these run in CI on a plain Linux box.

Run: pytest tests/test_scheduler.py
"""
import threading
import time

from ciqkit.sim import broker as broker_mod
from ciqkit.sim.broker import Broker
from ciqkit.sim.protocol import Job, Chunk, ChunkKind, Priority, JobState, Op
from ciqkit.sim.session import app_identity

broker_mod.leaselib = None  # no real 'sim' lease in unit tests


class FakeSession:
    """Records the order chunks execute in; small sleeps make timing realistic."""
    def __init__(self):
        self.current = None
        self.log = []          # list of (job_label, kind)
        self.lock = threading.Lock()
        self.act_s = 0.12
        self.act_s_by_label = {}   # label -> seconds, overrides act_s
        self.act_fails = False
        self.busy = []         # (start, end) of every session call
        self.seed_s = 0.0      # how long a seed takes to land
        self.seeds = 0
        self.restores = 0
        self.restarts = 0
        self.seeded = False
        self.builds = []       # (job_label, seeded_at_build_time)
        self.pushes = []       # (job_label, prg) of every load()
        self.reach_s = 0.0

    def _rec(self, kind):
        with self.lock:
            self.log.append((getattr(self, "_label", "?"), kind))

    def _span(self, t0):
        """Record when this call occupied the session. Two overlapping spans
        mean two threads drove the simulator at once."""
        with self.lock:
            self.busy.append((t0, time.time()))

    def ensure(self, project="."):
        return {"ok": True}

    def build(self, *a, **k):
        # Who built, and whether another job's seed was patched in at the time.
        with self.lock:
            self.builds.append((getattr(self, "_label", "?"), self.seeded))
        return "fake.prg"

    def load(self, app, prg, device="venu3", **k):
        t0 = time.time()
        # Key the loaded app the way the real session does, so the broker's
        # reload check is exercised rather than bypassed.
        self.current = (app_identity(app, project=k.get("project"), prg=prg),
                        device)
        self._rec("LOAD:" + app)
        with self.lock:
            self.pushes.append((getattr(self, "_label", "?"), prg))
        self._span(t0)
        return {"ok": True}

    def is_loaded(self, key, device):
        return self.current == (key, device)

    def act(self, *a, **k):
        t0 = time.time()
        self._rec("ACT")
        if self.act_fails:
            self._span(t0)
            return {"ok": False, "confirmed": False, "reason": "no-change"}
        time.sleep(self.act_s_by_label.get(getattr(self, "_label", None),
                                           self.act_s))
        self._span(t0)
        return {"ok": True, "confirmed": True}

    def shot(self, name, **k):
        t0 = time.time()
        self._rec("SHOT:" + name)
        self._span(t0)
        return {"ok": True, "path": name}

    def seed(self, app_dir, consts=None, patches=None, force=False):
        self._rec("SEED")
        time.sleep(self.seed_s)
        with self.lock:
            self.seeds += 1
        self.seeded = True
        return {"ok": True}

    def reach(self, steps, anchor=None, screen=None):
        t0 = time.time()
        self._rec("REACH")
        time.sleep(self.reach_s)
        self._span(t0)
        return {"ok": True, "screen": screen}

    def run_tests(self, prg, device="venu3", timeout_s=300):
        self._rec("TEST")
        time.sleep(0.3)
        return {"ok": True, "passed": 1, "failed": 0}

    def restore(self):
        with self.lock:
            self.restores += 1
        self.seeded = False
        return {"ok": True}

    def healthy(self):
        return True

    def restart(self):
        with self.lock:
            self.restarts += 1
        self.current = None
        return {"ok": True}

    def _capture(self):
        return None


def _mk(app, label, prio, n_acts):
    chunks = [Chunk(ChunkKind.LOAD, {"prg": "x.prg", "device": "venu3"},
                    expected_s=0.1, max_s=5)]
    for _ in range(n_acts):
        chunks.append(Chunk(ChunkKind.ACT, {}, expected_s=0.1, max_s=5))
    return Job(app=app, label=label, priority=prio, chunks=chunks)


def _run_broker():
    fake = FakeSession()
    b = Broker(session=fake)
    # tag the label onto the fake before each chunk via a wrapper on _dispatch
    orig = b._dispatch

    def dispatch(job, chunk):
        fake._label = job.label
        return orig(job, chunk)
    b._dispatch = dispatch
    th = threading.Thread(target=b.scheduler, daemon=True)
    th.start()
    return b, fake, th


def _wait_done(b, jid, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        j = b.jobs.get(jid)
        if j and j.state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED):
            return j
        time.sleep(0.02)
    raise AssertionError(f"job {jid} did not finish")


def test_urgent_preempts_batch():
    b, fake, _ = _run_broker()
    batch = _mk("appB", "batch", Priority.BATCH, 6)
    b.handle({"op": Op.SUBMIT, "job": batch.to_dict()})
    time.sleep(0.25)  # let batch start
    urgent = _mk("appU", "urgent", Priority.URGENT, 2)
    b.handle({"op": Op.SUBMIT, "job": urgent.to_dict()})
    _wait_done(b, batch.id)
    _wait_done(b, urgent.id)
    b.handle({"op": Op.SHUTDOWN})

    seq = [lbl for (lbl, kind) in fake.log if kind == "ACT"]
    # Urgent's 2 acts must both complete before batch's final act.
    last_urgent = max(i for i, lbl2 in enumerate(seq) if lbl2 == "urgent")
    last_batch = max(i for i, lbl2 in enumerate(seq) if lbl2 == "batch")
    assert last_urgent < last_batch, f"urgent did not preempt: {seq}"
    # Batch must have paused (some batch acts before urgent, some after).
    b_positions = [i for i, lbl2 in enumerate(seq) if lbl2 == "batch"]
    u_positions = [i for i, lbl2 in enumerate(seq) if lbl2 == "urgent"]
    assert min(b_positions) < min(u_positions) < max(b_positions), \
        f"no interleave (batch not preempted mid-run): {seq}"
    # Resume must have re-loaded batch's app (current was appU after urgent).
    loads = [k for (_l, k) in fake.log if k.startswith("LOAD:appB")]
    assert len(loads) >= 2, f"batch app not reloaded on resume: {fake.log}"
    print("PASS test_urgent_preempts_batch:", seq)


def test_bump_reorders():
    b, fake, _ = _run_broker()
    a = _mk("appA", "A", Priority.NORMAL, 6)
    small = _mk("appS", "S", Priority.BATCH, 2)   # behind A (batch < normal)
    b.handle({"op": Op.SUBMIT, "job": a.to_dict()})
    time.sleep(0.05)
    b.handle({"op": Op.SUBMIT, "job": small.to_dict()})
    time.sleep(0.25)  # A running, S waiting
    b.handle({"op": Op.BUMP, "job_id": small.id, "priority": Priority.URGENT})
    _wait_done(b, small.id)
    _wait_done(b, a.id)
    b.handle({"op": Op.SHUTDOWN})

    seq = [lbl for (lbl, kind) in fake.log if kind == "ACT"]
    last_s = max(i for i, lbl2 in enumerate(seq) if lbl2 == "S")
    last_a = max(i for i, lbl2 in enumerate(seq) if lbl2 == "A")
    assert last_s < last_a, f"bump did not reorder: {seq}"
    print("PASS test_bump_reorders:", seq)


def test_fifo_within_tier():
    b, fake, _ = _run_broker()
    j1 = _mk("app1", "J1", Priority.NORMAL, 2)
    j2 = _mk("app2", "J2", Priority.NORMAL, 2)
    b.handle({"op": Op.SUBMIT, "job": j1.to_dict()})
    b.handle({"op": Op.SUBMIT, "job": j2.to_dict()})
    _wait_done(b, j1.id)
    _wait_done(b, j2.id)
    b.handle({"op": Op.SHUTDOWN})
    seq = [lbl for (lbl, kind) in fake.log if kind == "ACT"]
    # J1 submitted first -> its acts should all precede J2's (same tier, FIFO,
    # no preemption between equal-priority jobs).
    assert seq == ["J1", "J1", "J2", "J2"], f"not FIFO within tier: {seq}"
    print("PASS test_fifo_within_tier:", seq)



# --- cancel: a cancelled job stays cancelled, and gives the source back ------

def _wait_for(pred, timeout=10, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError(f"{what} never happened")


def test_cancel_is_not_overwritten_by_the_chunk_that_was_in_flight():
    """The chunk boundary is the only preemption point, so a cancel lands while
    a chunk is still running. Reporting that job back as DONE tells the user the
    cancel failed *and* the work succeeded -- both wrong."""
    b, fake, _ = _run_broker()
    fake.act_s_by_label["C"] = 0.6
    job = _mk("appC", "C", Priority.NORMAL, 1)
    b.handle({"op": Op.SUBMIT, "job": job.to_dict()})
    _wait_for(lambda: any(k == "ACT" for (_l, k) in fake.log), what="the act started")
    b.handle({"op": Op.CANCEL, "job_id": job.id})
    # Deliberately not _wait_done: that returns the instant CANCELLED is set,
    # which is *before* the in-flight chunk lands. The overwrite happens when it
    # does, so wait for the chunk itself to finish.
    _wait_for(lambda: len(fake.busy) >= 2, what="the in-flight act finished")
    time.sleep(0.2)
    b.handle({"op": Op.SHUTDOWN})
    assert b.jobs[job.id].state == JobState.CANCELLED, \
        f"cancel was overwritten by {b.jobs[job.id].state}"


def test_cancel_stops_the_job_at_the_next_chunk_boundary():
    b, fake, _ = _run_broker()
    fake.act_s_by_label["C"] = 0.4
    job = _mk("appC", "C", Priority.NORMAL, 4)
    b.handle({"op": Op.SUBMIT, "job": job.to_dict()})
    _wait_for(lambda: any(k == "ACT" for (_l, k) in fake.log), what="the act started")
    b.handle({"op": Op.CANCEL, "job_id": job.id})
    _wait_done(b, job.id)
    time.sleep(0.6)   # long enough for another act to have slipped through
    b.handle({"op": Op.SHUTDOWN})
    acts = [k for (_l, k) in fake.log if k == "ACT"]
    assert len(acts) == 1, f"cancel did not stop the remaining chunks: {fake.log}"


def test_cancelling_a_seeded_job_puts_the_source_back():
    """Otherwise the user's Constants.mc stays patched until some unrelated job
    happens to finish -- and `ciqkit sim restore` refuses while the broker that
    owns the journal is alive, so there is no way out."""
    b, fake, _ = _run_broker()
    fake.act_s_by_label["S"] = 0.4
    job = Job(app="appS", label="S", priority=Priority.NORMAL, chunks=[
        Chunk(ChunkKind.SEED, {"app_dir": "/tmp/x", "consts": {"SEED_A": "true"}},
              expected_s=0.1, max_s=5),
        Chunk(ChunkKind.LOAD, {"prg": "x.prg", "device": "venu3"},
              expected_s=0.1, max_s=5),
        Chunk(ChunkKind.ACT, {}, expected_s=0.1, max_s=5),
        Chunk(ChunkKind.ACT, {}, expected_s=0.1, max_s=5),
    ])
    b.handle({"op": Op.SUBMIT, "job": job.to_dict()})
    _wait_for(lambda: fake.seeded, what="the seed applied")
    b.handle({"op": Op.CANCEL, "job_id": job.id})
    _wait_done(b, job.id)
    _wait_for(lambda: not fake.seeded, what="the seed restored")
    b.handle({"op": Op.SHUTDOWN})


def test_a_paused_jobs_seed_survives_another_jobs_completion():
    """Only the job that applied a patch may undo it. Restoring whenever *any*
    job ends would unseed a job that is merely paused between chunks, and its
    next build would silently come out unseeded."""
    b, fake, _ = _run_broker()
    fake.act_s_by_label["seeded"] = 0.25
    seeded = Job(app="appS", label="seeded", priority=Priority.BATCH, chunks=[
        Chunk(ChunkKind.SEED, {"app_dir": "/tmp/x", "consts": {"SEED_A": "true"}},
              expected_s=0.1, max_s=5),
        Chunk(ChunkKind.LOAD, {"prg": "x.prg", "device": "venu3"},
              expected_s=0.1, max_s=5),
    ] + [Chunk(ChunkKind.ACT, {}, expected_s=0.1, max_s=5) for _ in range(4)])
    b.handle({"op": Op.SUBMIT, "job": seeded.to_dict()})
    _wait_for(lambda: fake.seeded, what="the seed applied")
    urgent = _mk("appU", "urgent", Priority.URGENT, 1)
    b.handle({"op": Op.SUBMIT, "job": urgent.to_dict()})
    _wait_done(b, urgent.id)
    assert fake.seeded, "an unrelated job's completion unseeded a paused job"
    _wait_done(b, seeded.id)
    b.handle({"op": Op.SHUTDOWN})
    assert not fake.seeded, "the seeding job did not restore when it ended"


# --- a timed-out chunk must not leave a second driver on the simulator -------

def test_a_timed_out_chunk_is_reaped_before_the_next_job_runs():
    """A chunk that blows its max_s leaves its worker thread inside SimSession
    calls. Running the next job's chunk while it is alive puts two threads on
    the GUI -- the one thing this architecture exists to prevent."""
    b, fake, _ = _run_broker()
    fake.act_s_by_label["slow"] = 0.8
    slow = Job(app="appSlow", label="slow", priority=Priority.NORMAL, chunks=[
        Chunk(ChunkKind.LOAD, {"prg": "x.prg", "device": "venu3"},
              expected_s=0.1, max_s=5),
        Chunk(ChunkKind.ACT, {}, expected_s=0.05, max_s=0.05),   # will time out
    ])
    b.handle({"op": Op.SUBMIT, "job": slow.to_dict()})
    _wait_for(lambda: any(k == "ACT" for (_l, k) in fake.log), what="the slow act started")
    nxt = _mk("appN", "next", Priority.URGENT, 2)
    b.handle({"op": Op.SUBMIT, "job": nxt.to_dict()})
    j = _wait_done(b, slow.id)
    _wait_done(b, nxt.id)
    b.handle({"op": Op.SHUTDOWN})

    assert j.state == JobState.FAILED and "exceeded max_s" in (j.error or "")
    assert fake.restarts >= 1, "the sim was not restarted to unblock the worker"
    spans = sorted(fake.busy)
    for (s0, e0), (s1, _e1) in zip(spans, spans[1:]):
        assert s1 >= e0 - 0.01, \
            f"two session calls overlapped after a chunk timeout: {spans}"


# --- the job table must not grow without bound -------------------------------

def test_long_finished_jobs_are_dropped_from_the_table(monkeypatch):
    monkeypatch.setattr(broker_mod, "JOB_RETENTION_S", 0.2)
    b, _fake, _ = _run_broker()
    first = _mk("app1", "first", Priority.NORMAL, 1)
    b.handle({"op": Op.SUBMIT, "job": first.to_dict()})
    _wait_done(b, first.id)
    time.sleep(0.35)
    second = _mk("app2", "second", Priority.NORMAL, 1)   # wakes the scheduler
    b.handle({"op": Op.SUBMIT, "job": second.to_dict()})
    _wait_done(b, second.id)
    b.handle({"op": Op.SHUTDOWN})
    assert first.id not in b.jobs, "a long-finished job was never reaped"
    ids = [j["id"] for j in b.handle({"op": Op.STATUS})["jobs"]]
    assert first.id not in ids


def test_a_seed_that_lands_after_its_chunk_timed_out_is_still_restored():
    """The nasty interleaving: a SEED blows its max_s, the job fails, and only
    *then* does the patch land in the tree. If the failed job were recorded as
    the patch's owner, its _finish had already been and gone, so nothing would
    ever restore it -- and every later seeded job would fail with "a seed is
    already active". Ownership is recorded on the scheduler thread precisely so
    a late patch stays unowned, and unowned means anyone's completion restores
    it."""
    b, fake, _ = _run_broker()
    fake.seed_s = 0.5
    job = Job(app="appS", label="S", priority=Priority.NORMAL, chunks=[
        Chunk(ChunkKind.SEED, {"app_dir": "/tmp/x", "consts": {"SEED_A": "true"}},
              expected_s=0.05, max_s=0.05),   # will time out
    ])
    b.handle({"op": Op.SUBMIT, "job": job.to_dict()})
    j = _wait_done(b, job.id)
    assert j.state == JobState.FAILED
    # A counter, not the flag: the restore follows the late patch closely
    # enough that polling for `seeded` would miss the window.
    _wait_for(lambda: fake.seeds == 1, timeout=5, what="the late seed landed")
    _wait_for(lambda: not fake.seeded, timeout=5,
              what="the late seed was restored")
    assert b._seed_job_id is None, "a dead job was left owning the patch"
    b.handle({"op": Op.SHUTDOWN})


# --- nav: the reach step must report what actually happened ------------------

def _reach(b, fake, steps, anchor=None, ocr_result=None):
    """Drive the real SimSession.reach against the FakeSession.

    reach() lives on the session, not the broker -- it is simulator work. Bound
    to the fake, this exercises the real recipe logic with no GUI.
    """
    from ciqkit.sim import ocr as ocr_mod
    from ciqkit.sim.session import SimSession
    orig = ocr_mod.contains_any
    ocr_mod.contains_any = lambda img, anchors: ocr_result
    try:
        return SimSession.reach(fake, steps, anchor=anchor, screen="settings")
    finally:
        ocr_mod.contains_any = orig


def test_a_gesture_that_did_not_land_stops_the_recipe():
    """Step results used to be discarded, so a nav whose every tap missed still
    reported that it had arrived."""
    b, fake, _ = _run_broker()
    fake.act_fails = True
    r = _reach(b, fake, [{"kind": "tap"}, {"kind": "tap"}, {"kind": "tap"}])
    b.handle({"op": Op.SHUTDOWN})
    assert r["ok"] is False
    # 1-based with a total: "step 0" was read as "the first step".
    assert "step 1 of 3" in r["error"] and "confirm" in r["error"]
    acts = [k for (_l, k) in fake.log if k == "ACT"]
    assert len(acts) == 1, "the recipe kept going after a gesture missed"


def test_an_unavailable_ocr_backend_does_not_fail_a_nav():
    """contains_any returns None with no backend. bool(None) is False, which
    made every anchored nav fail on a machine without OCR."""
    b, fake, _ = _run_broker()
    r = _reach(b, fake, [{"kind": "tap"}], anchor=["SETTINGS"], ocr_result=None)
    b.handle({"op": Op.SHUTDOWN})
    assert r["ok"] is True and r["anchor"] is None


def test_a_contradicted_anchor_fails_the_nav():
    b, fake, _ = _run_broker()
    r = _reach(b, fake, [{"kind": "tap"}], anchor=["SETTINGS"], ocr_result=False)
    b.handle({"op": Op.SHUTDOWN})
    assert r["ok"] is False and r["anchor"] is False


def test_a_matched_anchor_passes_the_nav():
    b, fake, _ = _run_broker()
    r = _reach(b, fake, [{"kind": "tap"}], anchor=["SETTINGS"], ocr_result=True)
    b.handle({"op": Op.SHUTDOWN})
    assert r["ok"] is True and r["anchor"] is True


# ── priority aging ────────────────────────────────────────────────────────────
def test_a_long_waiting_batch_job_never_outranks_a_fresh_urgent_one():
    """Aging must not promote a job all the way to URGENT.

    It used to: aging floored at URGENT (0), so any job that had waited four
    tiers compared EQUAL to a real urgent job and `_runnable`'s created
    tie-break then handed the simulator to the older one. A regression run
    queued three minutes ago beat a screenshot submitted a second ago -- the
    exact inversion of the documented guarantee.
    """
    now = time.time()
    old_batch = Job(app="b", label="batch", priority=Priority.BATCH, chunks=[])
    old_batch.waiting_since = now - 3600           # waited an hour
    fresh_urgent = Job(app="u", label="urgent", priority=Priority.URGENT, chunks=[])
    fresh_urgent.waiting_since = now

    assert fresh_urgent.effective_priority(now) < old_batch.effective_priority(now)
    # ...and the aged batch job still overtakes an equally fresh NORMAL one,
    # which is what aging is actually for.
    fresh_normal = Job(app="n", label="normal", priority=Priority.NORMAL, chunks=[])
    fresh_normal.waiting_since = now
    assert old_batch.effective_priority(now) < fresh_normal.effective_priority(now)


def test_aging_counts_waiting_time_not_running_time():
    """A job that holds the simulator must not promote itself while it runs."""
    now = time.time()
    j = Job(app="b", label="batch", priority=Priority.BATCH, chunks=[])
    j.waiting_since = now - 90                    # waited two tiers
    j.mark_running(now)
    banked = j.effective_priority(now)
    # An hour of RUNNING later, its priority has not moved.
    assert j.effective_priority(now + 3600) == banked


def test_the_broker_ignores_a_client_supplied_aging_clock():
    """`created` arrives over the wire; the aging clock must not.

    A job dict echoed by `status` (or hand-rolled) carries a `created` far in
    the past. If that drove aging, re-submitting one would hand it permanent
    top priority.
    """
    b, _fake, _ = _run_broker()
    stale = _mk("appX", "X", Priority.BATCH, 1)
    d = stale.to_dict()
    d["created"] = time.time() - 86400
    d["waiting_since"] = time.time() - 86400
    d["waited_total"] = 86400.0
    b.handle({"op": Op.SUBMIT, "job": d})
    accepted = b.jobs[stale.id]
    assert accepted.waited_s() < 5.0, "client-supplied waiting time was trusted"
    assert accepted.effective_priority() == float(Priority.BATCH)
    b.handle({"op": Op.SHUTDOWN})


# ── the pre-chunk reload ──────────────────────────────────────────────────────
def test_a_wedged_reload_does_not_stall_the_whole_broker():
    """The reload before a screen-touching chunk must respect its own max_s.

    It used to run inline on the scheduler thread: no budget, no preemption
    point, no zombie recovery. A reload is a full build + push + verify, so a
    simulator that wedged there stalled every queued job for minutes -- and it
    fires right after a preemption, when an interactive job is already waiting.
    """
    b, fake, _ = _run_broker()
    slow = threading.Event()

    def wedged_load(app, prg, device="venu3", **k):
        fake._rec("LOAD:" + app)
        if app == "appB":
            slow.wait(30)     # never released within the test
        fake.current = (app_identity(app, project=k.get("project"), prg=prg),
                        device)
        return {"ok": True}

    batch = _mk("appB", "batch", Priority.BATCH, 4)
    for c in batch.chunks:
        c.max_s = 0.4
    urgent = _mk("appU", "urgent", Priority.URGENT, 1)

    b.handle({"op": Op.SUBMIT, "job": batch.to_dict()})
    time.sleep(0.3)                       # batch is running
    fake.load = wedged_load               # its RESUME reload will wedge
    b.handle({"op": Op.SUBMIT, "job": urgent.to_dict()})

    # The urgent job must still complete: batch's wedged reload is bounded and
    # fails its own job rather than the broker.
    _wait_done(b, urgent.id, timeout=25)
    j = _wait_done(b, batch.id, timeout=25)
    slow.set()
    b.handle({"op": Op.SHUTDOWN})
    assert j.state == JobState.FAILED
    # The reload is the job's own LOAD, rewound to and re-run under its budget.
    assert j.error == "chunk 'load' exceeded max_s=0.4s", j.error
    assert j.chunk_ix == 0, "the job did not rewind to its LOAD"


def test_a_failed_reload_wakes_waiters_immediately():
    """The reload path used to fail a job without the lock, and so without a
    `cond.notify_all()`. A client blocked in WAIT then learned nothing until
    the next 2 s poll tick -- it got the right answer by accident, late.
    """
    b, fake, _ = _run_broker()

    def boom(*a, **k):
        fake._rec("LOAD:boom")
        raise RuntimeError("push failed")

    fake.act_s = 0.3
    job = _mk("appB", "B", Priority.NORMAL, 6)
    b.handle({"op": Op.SUBMIT, "job": job.to_dict()})
    time.sleep(0.4)                       # let the first LOAD + ACT run
    fake.load = boom
    fake.current = None                   # force a reload before the next ACT

    t0 = time.time()
    r = b._wait(job.id, timeout_s=10)     # blocks on the condition variable
    elapsed = time.time() - t0
    b.handle({"op": Op.SHUTDOWN})

    assert r["ok"] is False
    assert r["job"]["error"] == "load: push failed", r["job"]["error"]
    assert elapsed < 1.5, \
        f"waiter was not notified; it only woke on the poll tick ({elapsed:.1f}s)"


# ── the wire is not the job table ─────────────────────────────────────────────
def test_a_client_cannot_submit_a_job_that_claims_to_be_finished():
    """`state` and `results` used to be read straight off the wire.

    `ciqkit sim status` emits exactly such a dict, and echoing one back to
    retry is the obvious thing to do -- so a job could arrive already DONE with
    a fabricated results list. The scheduler skipped it as terminal and WAIT
    returned ok=True in microseconds, quoting a screenshot path that was never
    captured. The simulator was never touched.
    """
    b, fake, _ = _run_broker()
    job = _mk("appX", "X", Priority.NORMAL, 1)
    d = job.to_dict()
    d["state"] = JobState.DONE
    d["results"] = [{"kind": "shot", "result": {"path": "/tmp/never-taken.png"}}]
    b.handle({"op": Op.SUBMIT, "job": d})

    j = _wait_done(b, job.id, timeout=15)
    b.handle({"op": Op.SHUTDOWN})
    assert j.state == JobState.DONE
    kinds = [r["kind"] for r in j.results]
    assert ChunkKind.LOAD in kinds and ChunkKind.ACT in kinds, \
        f"the job was skipped rather than run: {j.results}"
    assert not any("never-taken" in str(r) for r in j.results), \
        "a fabricated result survived submission"


def test_a_client_cannot_submit_a_job_that_skips_its_own_chunks():
    """`chunk_ix` off the wire let a job start past its SEED and LOAD."""
    b, fake, _ = _run_broker()
    job = _mk("appY", "Y", Priority.NORMAL, 1)
    d = job.to_dict()
    d["chunk_ix"] = len(job.chunks)      # "already done everything"
    b.handle({"op": Op.SUBMIT, "job": d})
    j = _wait_done(b, job.id, timeout=15)
    b.handle({"op": Op.SHUTDOWN})
    assert b.jobs[job.id].chunk_ix == len(job.chunks)
    assert [r["kind"] for r in j.results] == [ChunkKind.LOAD, ChunkKind.ACT], \
        f"chunks were skipped: {j.results}"


# ── the sim lease ─────────────────────────────────────────────────────────────
def test_a_job_fails_rather_than_driving_a_simulator_someone_else_holds():
    """The broker used to log a warning on LeaseTimeout and drive anyway.

    That voided the lease's whole contract -- "scripts that drive the simulator
    without going through the broker still serialise against it" -- at exactly
    the moment it mattered, since a timeout means someone else is on the GUI
    right now.
    """
    class _Boom(RuntimeError):
        pass

    class _FakeLeases:
        LeaseTimeout = _Boom

        @staticmethod
        def lease(*a, **k):
            raise _Boom("held by someone else")

        @staticmethod
        def status():
            return [{"resource": "sim", "pid": 4242, "who": "another-run",
                     "alive": True}]

    b, fake, _ = _run_broker()
    b._sim_lease_cm = None
    broker_mod.leaselib = _FakeLeases
    try:
        job = _mk("appL", "L", Priority.NORMAL, 1)
        b.handle({"op": Op.SUBMIT, "job": job.to_dict()})
        j = _wait_done(b, job.id, timeout=15)
    finally:
        broker_mod.leaselib = None
        b.handle({"op": Op.SHUTDOWN})
    assert j.state == JobState.FAILED
    assert "lease" in (j.error or "") and "4242" in (j.error or ""), j.error
    assert not fake.log, f"the simulator was driven anyway: {fake.log}"


# ── scheduler-thread survival ─────────────────────────────────────────────────
def test_a_systemexit_from_the_session_does_not_silently_kill_the_scheduler():
    """The capture layer used to `sys.exit(1)` on a failed simulator launch.

    SystemExit is a BaseException, so `_reap_zombie`'s `except Exception`
    guard let it through and killed the scheduler thread. The daemon stayed up
    and kept accepting jobs it would never run. Now it must fail the queue and
    stop instead of hanging.
    """
    b, fake, th = _run_broker()

    def boom():
        raise SystemExit(1)
    fake.restart = boom
    # A chunk that blows its max_s hands its worker to the reaper, which
    # restarts the simulator -- the path that used to lose the thread.
    fake.act_s = 5.0
    job = _mk("appZ", "Z", Priority.NORMAL, 1)
    job.chunks[-1].max_s = 0.2
    b.handle({"op": Op.SUBMIT, "job": job.to_dict()})

    th.join(timeout=20)
    assert not th.is_alive(), "scheduler thread is wedged"
    j = b.jobs[job.id]
    assert j.state in (JobState.FAILED, JobState.CANCELLED), \
        f"job left in {j.state}; a client would wait on it forever"
    assert b._stop, "broker did not stop after losing its scheduler"


if __name__ == "__main__":
    test_fifo_within_tier()
    test_urgent_preempts_batch()
    test_bump_reorders()
    print("\nALL SCHEDULER TESTS PASSED")


def _test_job(result):
    class S(FakeSession):
        def run_tests(self, prg, device="venu3", timeout_s=300):
            return result
    b = Broker()
    b.session = S()
    th = threading.Thread(target=b.scheduler, daemon=True)
    th.start()
    job = Job(app="a", label="t", priority=Priority.BATCH,
              chunks=[Chunk(ChunkKind.TEST_BATCH, {"prg": "x.prg"},
                            expected_s=0.1, max_s=5)])
    jid = b.handle({"op": Op.SUBMIT, "job": job.to_dict()})["job_id"]
    j = _wait_done(b, jid)
    b.handle({"op": Op.SHUTDOWN})
    return j


def test_a_green_test_chunk_finishes_the_job():
    assert _test_job({"ok": True, "passed": 1, "failed": 0}).state == JobState.DONE


def test_an_unreachable_simulator_fails_the_job_but_keeps_the_result():
    res = {"ok": False, "reason": "simulator-unreachable", "passed": 0, "failed": 0}
    j = _test_job(res)
    assert j.state == JobState.FAILED
    assert "simulator-unreachable" in j.error
    assert j.results[-1]["result"] == res


def test_failing_zero_and_malformed_test_results_fail_the_job():
    for res in ({"ok": False, "passed": 2, "failed": 1, "reason": None},
                {"ok": False, "reason": "zero-tests"},
                {"ok": False, "reason": "timeout"}, None, {}):
        assert _test_job(res).state == JobState.FAILED, res


# ── a seed is not chunk-local, and a checkout is not a build ─────────────────

def test_another_jobs_build_of_a_seeded_tree_waits_for_the_restore():
    """A seed stays patched into the source across chunk boundaries. An urgent
    job that preempted in that window built the tree with the other job's
    SEED_* values in it, and reported its screenshot as verified."""
    b, fake, _ = _run_broker()
    fake.act_s_by_label["seeded"] = 0.15
    seeded = Job(app="appP", label="seeded", priority=Priority.BATCH, chunks=[
        Chunk(ChunkKind.SEED, {"app_dir": "/tmp/projP", "consts": {"SEED_A": "true"}},
              expected_s=0.1, max_s=5),
        Chunk(ChunkKind.LOAD, {"app_dir": "/tmp/projP", "device": "venu3"},
              expected_s=0.1, max_s=5),
    ] + [Chunk(ChunkKind.ACT, {}, expected_s=0.1, max_s=5) for _ in range(3)] + [
        Chunk(ChunkKind.RESTORE, {}, expected_s=0.1, max_s=5),
    ])
    b.handle({"op": Op.SUBMIT, "job": seeded.to_dict()})
    _wait_for(lambda: fake.seeded, what="the seed applied")
    plain = Job(app="appP", label="plain", priority=Priority.URGENT, chunks=[
        Chunk(ChunkKind.LOAD, {"app_dir": "/tmp/projP", "device": "venu3"},
              expected_s=0.1, max_s=5),
        Chunk(ChunkKind.SHOT, {"name": "p"}, expected_s=0.1, max_s=5),
    ])
    b.handle({"op": Op.SUBMIT, "job": plain.to_dict()})
    assert _wait_done(b, plain.id).state == JobState.DONE
    assert _wait_done(b, seeded.id).state == JobState.DONE
    b.handle({"op": Op.SHUTDOWN})
    assert ("plain", False) in fake.builds and ("plain", True) not in fake.builds, \
        f"the unseeded job built the other job's seeded tree: {fake.builds}"


def test_a_seeded_tree_does_not_hold_up_unrelated_work():
    """The seed window blocks only what it would corrupt: a pre-built .prg on
    another project still preempts as usual."""
    b, fake, _ = _run_broker()
    fake.act_s_by_label["seeded"] = 0.25
    seeded = Job(app="appP", label="seeded", priority=Priority.BATCH, chunks=[
        Chunk(ChunkKind.SEED, {"app_dir": "/tmp/projP", "consts": {"SEED_A": "true"}},
              expected_s=0.1, max_s=5),
        Chunk(ChunkKind.LOAD, {"app_dir": "/tmp/projP", "device": "venu3"},
              expected_s=0.1, max_s=5),
    ] + [Chunk(ChunkKind.ACT, {}, expected_s=0.1, max_s=5) for _ in range(4)] + [
        Chunk(ChunkKind.RESTORE, {}, expected_s=0.1, max_s=5),
    ])
    b.handle({"op": Op.SUBMIT, "job": seeded.to_dict()})
    _wait_for(lambda: fake.seeded, what="the seed applied")
    urgent = _mk("appU", "urgent", Priority.URGENT, 1)
    b.handle({"op": Op.SUBMIT, "job": urgent.to_dict()})
    _wait_done(b, urgent.id)
    assert b.jobs[seeded.id].state not in (JobState.DONE, JobState.FAILED), \
        "the urgent job waited for the whole seeded job instead of preempting it"
    _wait_done(b, seeded.id)
    b.handle({"op": Op.SHUTDOWN})


def test_a_resumed_job_reloads_after_another_build_of_the_same_checkout():
    """Two jobs on one project share the loaded-app key, so "my app is loaded"
    was true after the other job pushed ITS build -- and the resumed job shot
    that build. Any push since a job's own LOAD forces a reload."""
    b, fake, _ = _run_broker()
    fake.act_s_by_label["first"] = 0.2
    first = Job(app="appP", label="first", priority=Priority.BATCH, chunks=[
        Chunk(ChunkKind.LOAD, {"prg": "/tmp/projP/bin/a.prg", "project": "/tmp/projP",
                               "device": "venu3"}, expected_s=0.1, max_s=5),
        Chunk(ChunkKind.ACT, {}, expected_s=0.1, max_s=5),
        Chunk(ChunkKind.SHOT, {"name": "a"}, expected_s=0.1, max_s=5),
    ])
    b.handle({"op": Op.SUBMIT, "job": first.to_dict()})
    _wait_for(lambda: ("first", "ACT") in fake.log, what="the first job's act")
    second = Job(app="appP", label="second", priority=Priority.URGENT, chunks=[
        Chunk(ChunkKind.LOAD, {"prg": "/tmp/projP/bin/b.prg", "project": "/tmp/projP",
                               "device": "venu3", "force": True},
              expected_s=0.1, max_s=5),
        Chunk(ChunkKind.SHOT, {"name": "b"}, expected_s=0.1, max_s=5),
    ])
    b.handle({"op": Op.SUBMIT, "job": second.to_dict()})
    _wait_done(b, second.id)
    _wait_done(b, first.id)
    b.handle({"op": Op.SHUTDOWN})
    assert fake.pushes[-1] == ("first", "/tmp/projP/bin/a.prg"), \
        f"the first job resumed on the second job's build: {fake.pushes}"
    shot_ix = fake.log.index(("first", "SHOT:a"))
    assert ("first", "LOAD:appP") in fake.log[fake.log.index(("second", "SHOT:b")):shot_ix]


def test_a_resumed_job_replays_its_navigation_before_the_shot():
    """Re-pushing the app is not a resume: it boots on its first screen. A job
    preempted between REACH and SHOT used to reload and then shoot the start
    screen under the target screen's name. It rewinds to its LOAD instead."""
    b, fake, _ = _run_broker()
    fake.reach_s = 0.3
    nav = Job(app="appA", label="nav", priority=Priority.BATCH, chunks=[
        Chunk(ChunkKind.LOAD, {"prg": "/tmp/projA/bin/a.prg", "project": "/tmp/projA",
                               "device": "venu3"}, expected_s=0.1, max_s=5),
        Chunk(ChunkKind.REACH, {"screen": "target", "steps": [{"kind": "tap"}]},
              expected_s=0.1, max_s=5),
        Chunk(ChunkKind.SHOT, {"name": "target"}, expected_s=0.1, max_s=5),
    ])
    b.handle({"op": Op.SUBMIT, "job": nav.to_dict()})
    _wait_for(lambda: ("nav", "REACH") in fake.log, what="the nav job's reach")
    b.handle({"op": Op.SUBMIT, "job": _mk("appU", "urgent", Priority.URGENT, 0).to_dict()})
    j = _wait_done(b, nav.id)
    b.handle({"op": Op.SHUTDOWN})
    assert j.state == JobState.DONE, j.error
    after = fake.log[fake.log.index(("urgent", "LOAD:appU")):]
    assert after.index(("nav", "LOAD:appA")) < after.index(("nav", "REACH")) \
        < after.index(("nav", "SHOT:target")), \
        f"the shot did not follow a fresh load + navigation: {fake.log}"
    assert [r["kind"] for r in j.results] == ["load", "reach", "shot"], j.results


def test_a_reload_rebuilds_instead_of_pushing_the_shared_output_path():
    """bin/NAME.prg is the same path for every device and every job. Caching it
    on the LOAD meant a resumed job pushed whatever was built there LAST -- here
    another job's fr965 build into a venu3 session."""
    b, fake, _ = _run_broker()
    fake.act_s_by_label["venu"] = 0.3
    n = [0]

    def build(app_dir, device="venu3", **k):
        n[0] += 1
        with fake.lock:
            fake.builds.append((fake._label, fake.seeded))
        return f"/tmp/projP/bin/{fake._label}-{device}-{n[0]}.prg"
    fake.build = build
    venu = Job(app="appP", label="venu", priority=Priority.BATCH, chunks=[
        Chunk(ChunkKind.LOAD, {"app_dir": "/tmp/projP", "project": "/tmp/projP",
                               "device": "venu3"}, expected_s=0.1, max_s=5),
        Chunk(ChunkKind.ACT, {}, expected_s=0.1, max_s=5),
        Chunk(ChunkKind.SHOT, {"name": "v"}, expected_s=0.1, max_s=5),
    ])
    b.handle({"op": Op.SUBMIT, "job": venu.to_dict()})
    _wait_for(lambda: ("venu", "ACT") in fake.log, what="the venu job's act")
    fr = Job(app="appP", label="fr", priority=Priority.URGENT, device="fr965", chunks=[
        Chunk(ChunkKind.LOAD, {"app_dir": "/tmp/projP", "project": "/tmp/projP",
                               "device": "fr965"}, expected_s=0.1, max_s=5),
        Chunk(ChunkKind.SHOT, {"name": "f"}, expected_s=0.1, max_s=5),
    ])
    b.handle({"op": Op.SUBMIT, "job": fr.to_dict()})
    _wait_done(b, fr.id)
    assert _wait_done(b, venu.id).state == JobState.DONE
    b.handle({"op": Op.SHUTDOWN})
    assert fake.pushes[-1] == ("venu", "/tmp/projP/bin/venu-venu3-3.prg"), \
        f"the resumed job did not push a fresh build of its own: {fake.pushes}"


def test_a_bumped_job_whose_reload_would_build_a_seeded_tree_waits():
    """The seed gate looked only at the literal next chunk. A job paused before
    an ACT, bumped while another job's seed was live, rebuilt that tree on its
    reload and shot the seeded app as its own."""
    b, fake, _ = _run_broker()
    fake.act_s_by_label["plain"] = 0.3
    fake.act_s_by_label["seeded"] = 0.15
    plain = Job(app="appP", label="plain", priority=Priority.NORMAL, chunks=[
        Chunk(ChunkKind.LOAD, {"app_dir": "/tmp/projP", "project": "/tmp/projP",
                               "device": "venu3"}, expected_s=0.1, max_s=5),
        Chunk(ChunkKind.ACT, {}, expected_s=0.1, max_s=5),
        Chunk(ChunkKind.SHOT, {"name": "p"}, expected_s=0.1, max_s=5),
    ])
    b.handle({"op": Op.SUBMIT, "job": plain.to_dict()})
    _wait_for(lambda: ("plain", "ACT") in fake.log, what="the plain job's act")
    seeded = Job(app="appP", label="seeded", priority=Priority.URGENT, chunks=[
        Chunk(ChunkKind.SEED, {"app_dir": "/tmp/projP", "consts": {"SEED_A": "true"}},
              expected_s=0.1, max_s=5),
        Chunk(ChunkKind.LOAD, {"app_dir": "/tmp/projP", "project": "/tmp/projP",
                               "device": "venu3"}, expected_s=0.1, max_s=5),
    ] + [Chunk(ChunkKind.ACT, {}, expected_s=0.1, max_s=5) for _ in range(3)] + [
        Chunk(ChunkKind.RESTORE, {}, expected_s=0.1, max_s=5),
    ])
    b.handle({"op": Op.SUBMIT, "job": seeded.to_dict()})
    _wait_for(lambda: ("seeded", "LOAD:appP") in fake.log, what="the seeded load")
    # Same tier, created earlier: without the gate it wins the next boundary.
    b.handle({"op": Op.BUMP, "job_id": plain.id, "priority": Priority.URGENT})
    assert _wait_done(b, plain.id).state == JobState.DONE
    assert _wait_done(b, seeded.id).state == JobState.DONE
    b.handle({"op": Op.SHUTDOWN})
    assert ("plain", True) not in fake.builds, \
        f"the bumped job rebuilt the other job's seeded tree: {fake.builds}"
    assert fake.builds.count(("plain", False)) == 2, fake.builds


def test_a_job_does_not_rewind_to_a_load_it_just_ran():
    """The session keys the loaded app by project or the .prg's directory; a
    LOAD with only app_dir used to be re-derived differently here, so the job
    found its own fresh push "not loaded" -- a rebuild before every gesture
    then, and with rewinding, a loop that never reached the next chunk."""
    b, fake, _ = _run_broker()
    job = Job(app="appP", label="solo", priority=Priority.NORMAL, chunks=[
        Chunk(ChunkKind.LOAD, {"app_dir": "/tmp/projP", "device": "venu3"},
              expected_s=0.1, max_s=5),
    ] + [Chunk(ChunkKind.ACT, {}, expected_s=0.1, max_s=5) for _ in range(3)])
    b.handle({"op": Op.SUBMIT, "job": job.to_dict()})
    assert _wait_done(b, job.id, timeout=5).state == JobState.DONE
    b.handle({"op": Op.SHUTDOWN})
    assert [k for (_l, k) in fake.log] == ["LOAD:appP"] + ["ACT"] * 3, fake.log


# ── stopping: never a second driver, never a silent drop ─────────────────────

def test_a_worker_that_cannot_be_stopped_stops_the_broker(monkeypatch):
    """After the grace period the broker used to "continue anyway" with the
    stuck worker still on the GUI -- running the next job beside it and, when
    idle, handing the `sim` lease to whoever wanted it."""
    monkeypatch.setattr(broker_mod, "ZOMBIE_GRACE_S", 0.3)
    b, fake, th = _run_broker()
    fake.act_s_by_label["stuck"] = 2.0          # restart() does not unblock it
    stuck = Job(app="appS", label="stuck", priority=Priority.NORMAL, chunks=[
        Chunk(ChunkKind.LOAD, {"prg": "x.prg", "device": "venu3"},
              expected_s=0.1, max_s=5),
        Chunk(ChunkKind.ACT, {}, expected_s=0.05, max_s=0.05),
    ])
    b.handle({"op": Op.SUBMIT, "job": stuck.to_dict()})
    _wait_for(lambda: ("stuck", "ACT") in fake.log, what="the stuck act started")
    nxt = _mk("appN", "next", Priority.URGENT, 1)
    b.handle({"op": Op.SUBMIT, "job": nxt.to_dict()})
    j = _wait_done(b, nxt.id)
    th.join(5)
    assert j.state == JobState.FAILED and "two threads" in (j.error or ""), j.error
    assert not th.is_alive() and b._stop, "the broker kept going"
    assert not any(lbl == "next" for (lbl, _k) in fake.log), \
        f"a chunk ran beside the stuck worker: {fake.log}"


def test_shutdown_fails_queued_jobs_and_ends_the_running_one_at_its_boundary():
    """SHUTDOWN only raised a flag: queued jobs were never told, and the process
    exited under the scheduler, so every WAIT ended in a dropped socket."""
    b, fake, th = _run_broker()
    fake.act_s_by_label["running"] = 0.4
    running = _mk("appR", "running", Priority.URGENT, 3)
    queued = _mk("appQ", "queued", Priority.BATCH, 1)
    b.handle({"op": Op.SUBMIT, "job": running.to_dict()})
    b.handle({"op": Op.SUBMIT, "job": queued.to_dict()})
    _wait_for(lambda: ("running", "ACT") in fake.log, what="the first act started")
    b.handle({"op": Op.SHUTDOWN})
    q = b.jobs[queued.id]
    assert q.state == JobState.FAILED and "shutting down" in (q.error or ""), q.error
    assert b.jobs[running.id].state == JobState.RUNNING, \
        "the job in flight was failed under its own running chunk"
    th.join(5)
    assert not th.is_alive()
    r = b.jobs[running.id]
    assert r.state == JobState.FAILED and "shut down" in (r.error or ""), r.error
    assert sum(1 for (lbl, k) in fake.log if lbl == "running" and k == "ACT") == 1, \
        "the scheduler started another chunk after SHUTDOWN"


def test_one_lease_timeout_fails_every_queued_job_at_once():
    """Each queued job used to pay the full lease timeout in turn against the
    same holder: ten screens, ten serial timeouts."""
    calls = []

    class _Boom(RuntimeError):
        pass

    class _FakeLeases:
        LeaseTimeout = _Boom

        @staticmethod
        def lease(*a, **k):
            calls.append(1)
            time.sleep(0.3)
            raise _Boom("held by someone else")

        @staticmethod
        def status():
            return []

    b, fake, _ = _run_broker()
    broker_mod.leaselib = _FakeLeases
    try:
        jobs = [_mk(f"app{i}", f"j{i}", Priority.NORMAL, 1) for i in range(3)]
        for job in jobs:
            b.handle({"op": Op.SUBMIT, "job": job.to_dict()})
        done = [_wait_done(b, job.id, timeout=10) for job in jobs]
    finally:
        broker_mod.leaselib = None
        b.handle({"op": Op.SHUTDOWN})
    assert all(j.state == JobState.FAILED and "lease" in (j.error or "") for j in done)
    assert len(calls) == 1, f"the lease was waited on {len(calls)} times"
    assert not fake.log
