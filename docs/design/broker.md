# Why the broker is shaped this way

Design notes for `ciqkit.sim`. Read [sim.md](../sim.md) first for how to use it;
this is the reasoning behind the shape.

## The constraint

The Connect IQ Simulator is a **single machine-wide GUI singleton**:

* one instance, running one app, for one device, at a time;
* driven by moving a real mouse pointer over a real window, so it also needs to
  be visible and unobscured;
* with no API — the only handle is `monkeydo` to push a `.prg`, plus whatever
  you can do to its window.

Every automation problem downstream follows from that.

## Three failures worth designing around

**1. Contention corrupts, silently.** A screenshot script and a test run that
start together do not fail loudly — they interleave taps into each other's app
and produce plausible-looking wrong output. Plain mutual exclusion fixes the
corruption but creates the next problem.

**2. Mutual exclusion is not enough.** A pure lock has no fairness and no
priority. A 20-minute regression run holds the simulator, and someone who needs
*one* screenshot from another project right now has no option but to kill it.
Wanting to jump the queue is the normal case, not an edge case.

**3. `monkeydo` returns before the app is up.** It pushes and exits immediately.
A script that sleeps and screenshots captures the *watchface* that was showing
before — and the PNG looks like a real screenshot, so nobody notices until much
later. Blind fractional taps then compound it: they land on whatever happens to
be under them.

## The response

### One owner

A single daemon constructs the one `SimSession`. Everything else is a client
over a unix socket. The single-actor invariant is structural rather than
conventional: there is no code path where two things drive the GUI, because only
the scheduler thread ever holds the session.

### Jobs made of chunks

A job is an ordered list of chunks (`load`, `shot`, `act`, `record`, `test`,
`seed`, `restore`). Chunks exist for one reason: **preemption boundaries**.

You cannot pause halfway through a `monkeydo`, but you can decide what runs next
*between* chunks. The scheduler re-selects the best job at every boundary, so a
job arriving mid-chunk waits at most one chunk rather than one job. That makes
chunk granularity a real design parameter — coarse enough to be a useful unit of
work, fine enough that preemption is responsive.

Preemption is cheap because chunks are restartable. A displaced job goes to
`paused` with its chunk index intact; when it resumes, `_ensure_loaded` replays
its most recent `LOAD` chunk if an intervening job changed what is on screen.

### Priority with ageing

Four tiers, lower is more urgent. Effective priority ages toward urgent at one
tier per 45 s of waiting, floored at `URGENT`. Without ageing, a steady stream
of interactive work starves batch jobs forever — which is exactly the state a
test suite would end up in on a busy machine.

### Verified everything

The lesson from failure 3 is that **an unverified action is worse than a failed
one**, because it produces confident wrong output.

So `load` proves the app replaced the watchface (the round face's brightness
signature changed from the pre-push frame, and the frame is not blank), and
optionally proves it is the *right* screen via an OCR anchor. `act` proves the
screen actually changed, or that the expected text appeared, and retries if not.

The frame-diff check is deliberately brightness-independent. An earlier
"does this look like app content" heuristic false-rejected both a white QR page
and a dark menu — and the recovery it triggered (restart the simulator on every
miss) churned the simulator into real wedges. Recovery now escalates: re-push,
then restart, three attempts.

OCR is a **confirmation, not a gate**. Small watch fonts miss sometimes; when an
anchor is supplied but never matches, the load is accepted once the frame is
stable and the result says `anchor_confirmed: false`. Without an OCR backend,
anchors are skipped. A check that fires spuriously gets disabled by users, and
then it protects nothing.

### Restore that survives the process

Seeding patches source files, so the interesting case is not the happy path but
every abnormal exit. Every touched file is backed up **byte-exactly in memory**
and restored verbatim — never regex-reversed, which does not round-trip.
Restore runs on job completion, on job failure, on `SIGTERM`/`SIGINT`, and from
an `atexit` hook, and it reports whether `git` agrees the file is clean again.

`SIGKILL` remains uncatchable. That is documented rather than papered over.

### A lease at the edge

While the queue is non-empty the daemon holds a machine-wide `sim` lease
(`ciqkit.leases`). That is not for ciqkit's own consistency — one owner already
guarantees that — but so scripts that have *not* been migrated to the broker
still serialise against it. The lease is the compatibility boundary; the broker
is the real mechanism.

Leases use `fcntl.flock`, not PID files or lock directories, because the kernel
releases an flock when the holder dies. There are no stale locks to reap, which
is the failure mode that makes hand-rolled locks unreliable. They are reentrant
across a process tree via an inherited environment variable, so a lease can live
both at the resource and at the call site without deadlocking itself.

## Deliberate non-goals

* **No UI-tree driving.** `nav` replays gestures you wrote and verifies the
  destination. Finding a button by name would need an accessibility tree the
  simulator does not expose.
* **No parallelism.** There is one simulator. Multiple simulator instances are
  not a supported Garmin configuration.
* **No persistence.** The job table is in memory. A crashed daemon loses the
  queue; every command is cheap to re-issue, and a persistent queue would add a
  failure mode worse than the one it removes.
* **No dependencies in the protocol layer.** `protocol.py` is stdlib-only JSON
  lines, so the wire format stays inspectable with `nc` and the package stays
  easy to embed.
