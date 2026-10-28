# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`ciqkit` — unofficial developer tooling for Garmin Connect IQ watch apps: a
scheduling broker in front of the Connect IQ Simulator, plus static image checks
on captured watch screens. macOS and Windows are the supported platforms (Garmin's
Linux simulator is a beta ciqkit has no window control for). macOS: window control via AppleScript,
capture via Quartz/`screencapture`. Windows: Win32 via ctypes — `PrintWindow`
capture, SendInput gestures. Linux runs only the static checks and the broker
core.

## Commands

```bash
pip install -e ".[dev,sim]"   # dev setup
pytest                        # full suite — no simulator, SDK, GUI or display needed
pytest tests/test_scheduler.py::test_urgent_preempts_batch -q   # single test
ruff check src tests          # lint (line-length 100)
ciqkit doctor                 # environment check; not run in CI
```

SDK MCP server (`tools/sdk-mcp`, separate Node project):

```bash
cd tools/sdk-mcp && npm install && npm run build   # must typecheck clean
npm run parse-sdk                                  # needs a local Connect IQ SDK
```

CI (`.github/workflows/ci.yml`) runs pytest + ruff on Linux, macOS and Windows × py3.11/3.13,
builds the Node server, validates `data/gotchas.json`, and enforces that **no
Garmin-derived material is ever committed** (`sdk-index.json`, `.iq/.prg/.der/.jks`).

## Architecture

Layered, one direction only:

```
cli.py  ──►  sim/client.py ──sim/transport.py (JSON lines)──►  sim/broker.py
             sim/mcp_server.py ──┘                          │ owns exactly one
             qa/{layout,shot_score,icons}.py                 ▼
             lease_cli.py                                 sim/session.py
                                                             ▼
                                     capture/screenshot_utils.py  (window+input)
                                       ├─ capture/geometry.py  (pure CV)
                                       └─ capture/framing.py   (store render)
```

* **`cli.py`** — dispatch only. Subcommands are lazily imported so a base install
  (zero dependencies, on purpose) reports "you need extra X" instead of a
  `ModuleNotFoundError` traceback. Extras: `sim`, `checks`, `cv`, `ocr`, `dev`.
* **`sim/protocol.py`** — the shared vocabulary: `Job` (ordered list of `Chunk`s
  + priority/device/owner), `ChunkKind`, `JobState`, `Op`, `Priority`. **Keep it
  stdlib-only and JSON-serialisable.** `Job.from_dict` reads only
  `Job.CLIENT_FIELDS`; `state`, `chunk_ix`, `results` and the timestamps are the
  daemon's own and are **not** readable from the wire (see the invariant below).
* **`sim/broker.py`** — the daemon. One scheduler thread runs jobs **one chunk at
  a time**, re-selecting the best job at every boundary; that boundary is the only
  preemption point. Displaced jobs go `PAUSED` and resume with `chunk_ix` intact,
  unless another job pushed meanwhile: then they **rewind** to their own LOAD
  and replay from there (a re-push boots the app's first screen, so gestures
  already done are gone).
  Priority ages toward urgent (one whole tier per 45 s of **waiting**, never
  reaching urgent itself) so batch jobs cannot starve while a real urgent job
  still preempts them. Every session call the scheduler makes runs on a
  bounded worker thread.
  Holds the machine-wide `sim` lease while busy, so unmigrated scripts still
  serialise against it — and **fails the job** if it cannot take it, rather
  than driving a simulator someone else is already on. A daemon-singleton
  `flock` beside the socket, not a probe-then-unlink of the socket file, is
  what stops two brokers starting at once.
* **`sim/session.py`** — the *only* thing that touches the GUI. Synchronous and
  single-threaded by design; all concurrency lives in the broker. Anything that
  drives gestures or reads the screen belongs here (`reach` included), not in
  the broker — the broker decides *when* work runs, not what a nav step is.
* **`sim/seed.py`** — seeding (`apply`/`restore`, path containment, git-dirty guard);
  `SimSession.seed`/`restore` only hold the backup between them. No GUI.
* **`sim/recording.py`** — pure helpers behind `record`/`run_tests`: GIF frame timing,
  contact sheet, test-output parser. Captures nothing.
* **`sim/errors.py`** — `SimError`, apart so `seed.py` needn't import `session.py`.
* **`sim/client.py`** — client library + `ciqkit sim` CLI. Auto-spawns the daemon
  ssh-agent style on first use.
* **`sim/mcp_server.py`** — hand-rolled JSON-RPC 2.0 over stdio (no MCP SDK
  dependency), exposing the same jobs as `ciqsim_*` tools.
* **`sim/transport.py`** — the control channel: a unix socket, or on Windows
  (no `AF_UNIX` in CPython) loopback TCP whose first line must carry a
  per-broker token from the private endpoint file. `CIQSIM_TRANSPORT=tcp`
  forces TCP on POSIX, which is how CI covers it there.
* **`paths.py` / `leases.py`** — runtime dir is private and ownership-checked:
  per-uid 0700 on POSIX, `%LOCALAPPDATA%\ciqkit\runtime` with a protected
  user+SYSTEM ACL on Windows. Leases are OS file locks (kernel-released, so no
  stale locks) and reentrant across a process tree via an inherited env var.
* **`_compat.py` / `_winapi.py`** — every primitive POSIX and Windows spell
  differently lives here: pid liveness (**never `os.kill(pid, 0)`** — on
  Windows 0 is CTRL_C_EVENT: dead pids read alive and the caller gets Ctrl+C),
  file locks (`fcntl` / `msvcrt`), detached spawning, and Win32 process/port
  tables (not localised `netstat` text). `winbash.py` finds Git for Windows'
  bash — never the WSL `bash.exe` first on PATH.
* **`qa/`** — pure image processing, no GUI, runs headless in CI. So are
  `capture/geometry.py` (face detection, crops, frame signatures) and
  `capture/framing.py` (store render); `screenshot_utils.py` keeps only what
  needs a real window, subprocess or platform API, and re-exports the other
  two so `su.*` stays the single entry point.

## Invariants worth not breaking

* **One actor drives the simulator.** Only the broker's scheduler thread holds a
  `SimSession`. Anything that would let a second thread or process touch the GUI
  breaks the whole design.
* **Chunks must be short and restartable.** They are the preemption unit; a long
  chunk means a long preemption latency.
* **A seed and a loaded build outlive their chunk.** A seed stays in the source
  from SEED to RESTORE, so while it is live `_runnable` holds back any other job
  that would build that tree or seed — judged on the chunk it will *really* run
  next, i.e. its LOAD when a rewind is due. And "this checkout is loaded" is
  not "my build is loaded": a job rewinds (forced LOAD) unless nothing was
  pushed since its own LOAD (`_load_gen`). A built LOAD always rebuilds: the
  output path is shared by every device, seed and job, so it is never cached.
* **Defaults and budgets have one owner.** Session-call defaults live in the
  `SimSession` signatures (`_dispatch` forwards only keys a chunk carries);
  shared ones sit in `protocol.py`. Front-ends never pick a wait timeout:
  `Client.submit_wait` derives it from the job's chunk budgets (`jobs.wait_budget`).
* **A worker that outlives its timeout stops the broker.** Python cannot kill a
  thread; if a sim restart does not free it within `CIQSIM_ZOMBIE_GRACE_S`, the
  broker fails every job and exits instead of running beside it.
* **Nothing is trusted unverified.** `load` proves the app replaced the watchface
  (frame not blank, not the CIQ loading screen, plus either the loader was seen to
  come and go or the round-face signature changed); `act` proves the screen
  changed. If the pre-push baseline frame cannot be captured, the
  signature-changed leg is *missing*, not satisfied — the load then needs the
  loader or an anchor, and fails otherwise. "Which app is loaded" is keyed on
  the resolved project path, not the directory basename, so two worktrees of
  one repo are two apps.
* **The wire cannot express a finished job.** A `status` dict looks exactly like
  a submission, and echoing one back to retry is the obvious thing to do — so
  `Job.from_dict` ignores every server-owned field. Accepting them let a client
  submit `state: "done"` with fabricated `results` and get `ok=True` back for a
  screenshot that was never captured.
* **Gestures are aimed in display fractions**, never pixels. `content_left` /
  `content_top` are in captured-image (physical) pixels while window sizes are
  logical; mixing the two is how every tap came to be off by the simulator's
  chrome. `capture_window` is the only thing that learns this geometry, and it
  is cached against the window rect so `_rebind()` cannot throw it away.
  On Windows the image is the DPI-unaware simulator's own drawing (0.5x the
  physical window at 200 %), so `retina_scale` is below 1 there, and fractions
  are of the located watch display (`set_display_box`), not the whole panel.
* **Windows input is real input into a focused panel.** The simulator ignores
  PostMessage'd mouse/keys, and keys only land while its per-push re-created
  `panel` child has focus. A locked session drops synthesized input, so
  gestures raise `SimulatorInputError` there instead of retrying blind.
* **Preflight caches a pass, never a failure.** Accessibility grants and SDK
  installs happen *while* the daemon runs; a cached "no" made the fix look like
  it had not taken for up to ten idle minutes. An unverified action producing confident wrong output is the failure
  mode this project exists to prevent.
* **OCR anchors are a confirmation, never a gate.** No backend, or a miss on a
  small font → `anchor_confirmed: false` / skipped, never a hard failure. (The CLI
  does exit 3 when an explicitly requested anchor is contradicted.)
* **Seeding restores byte-exactly.** Originals are held in memory and written back
  verbatim — never regex-reversed. Restore runs on completion, failure,
  SIGTERM/SIGINT and `atexit`, and is journaled (`sim/seedjournal.py`) so a dead
  process's patch is recoverable and visible in `ciqkit doctor`.
* **Never `sys.exit` from library code either.** `SystemExit` is a
  `BaseException`, so an `except Exception` guard lets it through, and on a
  worker or scheduler thread that is a silent death. The capture layer raises
  `SimulatorLaunchError`; the scheduler thread is additionally wrapped so that
  losing it stops the daemon instead of leaving one that accepts jobs it will
  never run.
* **Never write to stdout from library code.** The CLI prints JSON there and the
  MCP server speaks JSON-RPC over it. Diagnostics go to `logging` →
  `<runtime dir>/brokerd.log`.
* **The socket is a command channel** (a job can build a project and patch its
  source), which is why every runtime path is private, per-uid and 0700. See
  SECURITY.md before touching `paths.py` or the socket.
* **No persistence in the broker.** The job table is in memory on purpose; every
  command is cheap to re-issue.

## Tests

Must run without a simulator, SDK or display. `tests/test_scheduler.py` shows the
pattern: a `FakeSession` records chunk execution order, which is enough to prove
priority ordering, preemption, resume and bump. Scheduler changes belong there.
Image-processing changes get a small **synthetic** image, not a committed real
screenshot. `tests/conftest.py` autouse-redirects `XDG_RUNTIME_DIR` so tests never
touch the developer's real runtime dir or race a live broker.

The suite runs on Windows too (CI does). Do not assume POSIX in a test: write
fixture files byte-exact (`write_bytes`, or text mode adds `\r`), compare paths
with `os.path.join` rather than `/`, set `USERPROFILE` alongside `HOME` when
faking `~`, and express "private" through the platform (mode bits on POSIX,
`_winapi.dacl_sddl` on Windows) rather than skipping the check. Build-script
tests run their `.sh` through Git Bash on Windows. With `core.autocrlf=true`
the working tree is CRLF; git normalises on commit.

## Environment variables

`CIQ_HOME` (SDK path) · `CIQKIT_BUILD_CMD` (default `./scripts/build.sh -d {device}`, else built-in monkeyc via `ciqkit/build.py`)
· `CIQKIT_DEV_KEY` (developer key; default `~/.config/ciqkit/developer_key.der`)
· `CIQKIT_BASH` · `CIQKIT_OCR_LANG` · `CIQSIM_TRANSPORT`
· `CIQSIM_SOCK` · `CIQSIM_IDLE_EXIT_S` (600) · `CIQSIM_SIM_LEASE_TIMEOUT_S` ·
`CIQSIM_ZOMBIE_GRACE_S` (60) · `CIQSIM_JOB_RETENTION_S` (900) · `CIQSIM_SHUTDOWN_GRACE_S` (20) ·
`CIQKIT_SESSION_LABEL` · `CIQKIT_LOG_LEVEL` (`DEBUG` adds per-frame face-detector
reasoning — first thing to enable when a screenshot comes out wrong) ·
`XDG_RUNTIME_DIR`.

## Scope limits (see docs/scope.md)

Two categories are declined regardless of quality: automation of any Garmin
**online service** (developer dashboard, Garmin Connect, crash-report backend),
and anything that **redistributes Garmin material** (SDK files, docs text, device
images, sample code). `tools/sdk-mcp` indexes the SDK on the user's own machine
and ships no Garmin content.

## Style

Comments explain *why*, especially where code looks odd — most odd-looking code
here works around a real simulator behaviour, and the next reader needs to know
which. The `S110` (try/except/pass) ruff exemptions in `pyproject.toml` are
deliberate and each site carries a comment: best-effort GUI steps whose failure
must not abort a capture in progress.

## waterplane conventions

This repo is driven via waterplane (remote phone control of the `claude` CLI). Follow:

- **Outbox** — save screenshots / artifacts meant for the phone into
  `.claude/outbox/` (or a top-level `.outbox/`). Only these surface in the
  phone's curated gallery; anything else is buried in the broad "this repo" tab.
- **Inbox** — files sent from the phone land in `.claude/inbox/`; read uploads
  from there.
- Both inbox and outbox are gitignored — never commit their contents.
