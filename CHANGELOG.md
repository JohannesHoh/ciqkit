# Changelog

Notable changes to ciqkit. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version is `0.x`, minor releases may contain breaking changes.

## [0.1.0] — unreleased

First public release. The code has real mileage on one macOS setup and one
Windows 11 setup but has not been exercised across many; expect rough edges on
others.

Not published yet: the PyPI and npm release workflow
(`.github/workflows/release.yml`) is written but has not been run. Until it
has, install from a clone (`pip install -e ".[sim]"`).

### Added

* `tools/sdk-mcp`: `CIQKIT_SDK_MCP_DATA_DIR` (absolute path) points the server at
  a project's own data directory — `gotchas.json` is read from there, and an
  `sdk-index.json` in it takes precedence over the user cache.
* `tools/sdk-mcp`: `npm run bundle` builds a single-file
  `dist/ciqkit-sdk-mcp.mjs` (esbuild) that runs without `node_modules`, for
  vendoring into a project.
* `tools/sdk-mcp`: a `CIQKIT_SDK_MCP_DATA_DIR` that is relative, or an absolute dir without
  `gotchas.json`, now logs a warning on stderr instead of silently serving 0 gotchas; CI smoke-tests
  the bundle (initialize + `garmin_get_gotchas`).
* Gotchas 052–055: VM Errors (Invalid Value, Too Many Timers, ...) are not caught
  by try/catch; about three concurrent `Timer.Timer`s per app; `requestUpdate()`
  from a covered view repaints the menu on top; `CustomMenuItem.draw()` runs per
  row per scroll frame.
* Gotcha 056: monkeyc does not flag an API above the target device's level:
  `WatchUi.getCurrentView()` (3.4.0, like `showToast()`) builds cleanly for an
  API 3.3 device and dies there with Symbol Not Found.
* CI rejects unknown keys in `gotchas.json`. Gotchas 049–051 carried their code
  samples under `wrongCode`/`correctCode`, which the server never rendered; they
  now use `codeWrong`/`codeCorrect`.
* FIT playback on load: `--fit FILE` on `shot` / `record` / `nav` (LOAD chunk `fit`, nav step
  `{"kind":"fit","path":...}`, `fit` on `ciqsim_shot` / `ciqsim_nav`). After the verified load
  the simulator's Activity Data player is driven (FIT/GPX Playable File, Load File, play) and
  playback is verified -- the window shows that file and its clock advanced -- or the load
  fails. macOS only.
* Explicit and edge swipes: nav/ACT `swipe` takes `from: [x, y]` and `dist` (display
  fractions) and `edge: left|right|top|bottom`. A bare swipe keeps the default centred flick.
* Console-marker shots: `ciqkit sim shot --on-marker TEXT`, `ciqkit sim shot-on --marker
  TEXT=NAME ...` and nav step `{"kind":"shot_on","marker":...,"name":...,"timeout":20}` take a
  shot when the app prints TEXT on its console. monkeydo's output is now kept in
  `console.log` (it used to be discarded) and read with a forward-only cursor.
* `ciqkit sim phone connected|disconnected` (and nav step `{"kind":"phone","state":"connected"}`):
  sets the simulated phone link via Settings > Connection Type > BLE, reads the menu back
  and reports `state` / `phoneConnected`. `DeviceSettings.phoneConnected` follows it.
* `ciqkit sim close-aux` (and nav step `{"kind":"close_aux"}`): closes the simulator's
  auxiliary windows -- notably the "Webpage Window" `Communications.openWebPage` opens.
  This also runs automatically before every gesture and shot (opt out: `CIQKIT_KEEP_AUX=1`).
* macOS dialog handling now recognises the crash reporter's "<app> quit unexpectedly"
  for simulator-related processes ("Connect IQ Device Simulator" -- presented by
  UserNotificationCenter with Reopen / Ignore / Report...) and clicks **Ignore** (never
  Reopen/Report). Step and shot results carry `dismissed` (and `simulator_crashed: true`
  for a crash) so a crash is never silent.
* Nav step `button` (`{"kind":"button","id":"enter"}`): presses a device's physical
  key (ids from its `simulator.json`) by clicking its spot on the device art;
  hold-keys such as `menu` are held ~1.6 s. On macOS `key` steps with `enter`/`esc`
  now route through it, because a Return keystroke is not delivered to the app as
  START (arrows are, and stay keystrokes).
* `ciqkit new` and `ciqkit init` — scaffold a watch app, watch face or data
  field (or the tic-tac-toe sample) from templates, or wire an existing project
  up: `ciqkit.toml`, `.mcp.json`, an agent-instructions block, an optional CI
  workflow. `init` is dry-run by default and idempotent. Templates are MIT-0.
* `examples/tictactoe` — a complete sample app (venu3, fr255, venusq2: touch
  and buttons, round and rectangular) with unit tests, MIT-0.
* [Tutorial](docs/tutorial/) and an [adoption guide](docs/adopt.md) for bringing
  an existing project under ciqkit.
* `ciqkit.toml` — per-project config: `[build]` (incl. `test_cmd`), `[sim]`,
  named `[screens]` with navigation steps and OCR anchors.
* `ciqkit sim shots --all` / `--screen NAME` and `ciqsim_shots` — capture the
  declared screens of a project in one job.
* MCP: `ciqsim_nav` (navigate to a screen or along steps) and `ciqsim_doctor`;
  `ciqkit doctor --project DIR` checks one project (manifest, devices, key,
  build) as well as the machine.
* Build without a project script: a built-in `monkeyc` build is the fallback
  when there is no `scripts/build.sh` or `CIQKIT_BUILD_CMD`; `ciqkit keygen`
  creates the developer signing key; the default device comes from the
  manifest (preferring one that is installed in your SDK). `sim test` builds
  its own `-t` variant when no `--prg` is given.
* Release plumbing: `.github/workflows/release.yml` (PyPI trusted publishing and
  npm with provenance, gated on CI, version-vs-tag checks), and
  `ciqkit-sdk-mcp` made `npx`-able (see below).
* CI guards `examples/` and the templates against builds, keys, screenshots and
  SDK data, and a test parses every shipped XML file (templates after
  substitution).

* `ciqkit sim` — simulator broker with priority scheduling, chunk-boundary
  preemption, verified app loading, screenshots, GIF recording, gesture
  navigation and unit-test runs.
* `ciqkit-mcp` — the broker exposed as MCP tools.
* `ciqkit layout` — geometry checks for captured watch screens.
* `ciqkit shot-score` — screenshot quality scoring.
* `ciqkit icon` — BETA-ribbon compositing and duplicate-icon detection.
* `ciqkit lease` — machine-wide named locks for your own scripts.
* `ciqkit doctor` — environment check.
* `ciqkit sim restore` — put back files a seed left patched when its process
  was killed uncatchably.
* `tools/sdk-mcp` — MCP server for Connect IQ API lookup, generating its index
  from the SDK installed on your own machine, plus 48 curated Monkey C gotchas.
  Packaged for npm as `ciqkit-sdk-mcp` (`npx ciqkit-sdk-mcp`; build the index
  with `npx -p ciqkit-sdk-mcp ciqkit-sdk-parse`). The index lives in
  `~/.cache/ciqkit/sdk-index.json` (`$XDG_CACHE_HOME` respected); the old
  `tools/sdk-mcp/data/sdk-index.json` is still read as a fallback.
* `ciqkit sim wait <job_id>` — re-attach to a job whose submitting command gave
  up waiting. The default wait is shorter than a queued build can take, and the
  job keeps running either way; without this the printed job id was a dead end.
  A `wait timeout` now also says so, and names the command to run.

* `ciqkit.sim.simport` — where the simulator is listening, what else sits on
  the ports monkeydo probes, and `--start` to launch a missing simulator and wait
  for its port. `exec-monkeydo` runs monkeydo through a probe-skipping shell
  shim when a foreign listener is in the way. See
  [docs/sim.md](docs/sim.md#the-simulator-port).

* `ciqkit storage` — journal, reset and replay the simulator's per-app
  storage. The simulator keeps every app's Storage/Properties in one shared
  folder keyed only by the PRG name, so each run inherits whatever the last
  same-named run left, and a retry overwrites the evidence of a crash. Every
  cold `load` and every `test` run now journals the app's storage first (last
  20 per app, in `~/.local/state/ciqkit/storage-journal`, on Windows
  `%LOCALAPPDATA%\ciqkit\storage-journal`); on a CIQ_LOG.YML
  entry for the app, or a crash screen, the pre-run snapshot, the post-run
  storage, CIQ_LOG and the PRG are pinned next to the shots and never rotated
  out. `load(storage_start="fresh"|"keep"|<fixture dir>)`, the `storage`
  LOAD arg and `ciqkit sim ... --storage` declare the start state -- **fresh
  (empty) by default**, for cold loads and test runs alike, so no run boots on
  another run's leftovers. `ciqkit storage restore` replays a crash.
  Storage follows the app UUID, not the PRG name: the simulator installs a
  push over the installed same-UUID app under that app's name, so all builds
  of an app (unit-test PRGs included) share one storage file. ciqkit learns
  UUID -> installed name after each push and re-applies the start state (and
  re-pushes once) when a push landed under another name. The per-app
  `DATA/MEDIA/OBJSTORE/<NAME>/` tree is journaled and reset too.
* `act(kind="hold")` — touch-and-hold (BehaviorDelegate.onHold / onMenu on
  touch-primary devices).
* **Windows support**, developed and tested against the SDK 8.4 simulator on
  Windows 11 (200 % display), and Windows added to CI. Everything runs there:
  the broker, `sim shot/record/nav/test`, the MCP servers, `lease`, `storage`
  and `doctor`. What it took:
  * the broker's control channel is loopback TCP authenticated by a per-broker
    256-bit token kept in the private runtime directory (CPython has no unix
    sockets on Windows); `CIQSIM_TRANSPORT=tcp` exercises it on POSIX CI too;
  * the private runtime directory is `%LOCALAPPDATA%\ciqkit\runtime` with a
    protected user+SYSTEM-only ACL, owner-checked on every use;
  * leases, the broker singleton and the storage journal lock with `msvcrt`
    byte-range locks; process liveness, process lists, command lines and
    listening ports come from Win32 (no `os.kill(pid, 0)`, no localised
    `netstat`/`tasklist` text, no `wmic`, which Windows 11 no longer ships);
  * capture is `PrintWindow` at the simulator's own DPI, cropped to its device
    panel: native watch pixels, correct when the window is covered, working
    while the PC is locked;
  * gestures are real input with the simulator foregrounded and its device
    panel focused (it ignores posted messages), the window moved on-screen
    when it hangs below the taskbar, and `x`/`y` aimed at the watch display;
    in a locked session they fail at once with that reason;
  * a push is proved by the device panel being re-created, so an unchanged
    rebuild verifies in seconds instead of timing out into a restart;
  * the probe-skipping monkeydo shim works there too (a batch file plus a
    relay that kills `shell.exe` with it), and `simport` flags a program
    bound to `127.0.0.1` on the simulator's own port, which Windows lets
    take monkeydo's connection;
  * monkeydo runs as its own java line, `.sh` build commands run under Git for
    Windows' bash (never WSL's), `ciqkit lease` resolves `.cmd` shims and
    `.sh` scripts, the daemon and simulator are started detached without
    console windows, and the MCP servers speak UTF-8/LF on stdio;
  * `--anchor` OCR uses the Windows OCR engine (`ciqkit[ocr]` installs
    `winocr` there); `doctor` checks Java, a locked session and the
    simulator's own Touch Screen setting.

### Fixed

* `button` steps now accept `id` (canonical) or `button` (alias) in both nav recipes and
  ACT chunks; one validator (`protocol.step_errors`) checks steps at submit time.
* macOS: the device window is chosen by title ("CIQ Simulator - ..."), not "window 1",
  and raised above aux windows, so a shot can no longer capture a web view.
* macOS: gesture fractions are now fractions of the watch DISPLAY (as on Windows), not of
  the whole window. The window draws the full watch body, so on fr265s y=0.42 landed on
  the title above the first button and taps "did not register".
* Preemption no longer leaks one job's seed into another job's build. While a
  seed is patched in, a job that would build that project (or seed) waits for
  the restore. A resumed job also reloads its own build if anything else was
  pushed, even from the same checkout.
* A resumed job no longer screenshots the wrong screen or the wrong build. If
  another job pushed while it was paused, it rewinds to its own load and
  replays its navigation, so a shot after `steps` is of the target screen,
  not the app's start screen. The reload rebuilds instead of re-pushing
  `bin/NAME.prg`, which another device's or a seeded build may have
  overwritten, and it waits while another job's seed is patched in.
* `sim shot`/`ciqsim_shot` (and record, nav, test) wait as long as the job's
  own budget plus queue slack, instead of a fixed 360/400 s that a slow build
  could exceed. An MCP wait timeout now says `still_running: true` instead of
  `"error": null`.
* A timed-out chunk whose worker survives a simulator restart now stops the
  broker. It used to keep running jobs beside the stuck worker.
* `sim shutdown` fails queued jobs with a reason, lets the chunk in flight
  finish (`CIQSIM_SHUTDOWN_GRACE_S`) and tears down on the scheduler thread. It
  used to drop every WAIT and restore seeds from the main thread mid-chunk.
  SIGTERM/SIGINT now take the same path; a second signal exits at once.
* A `sim` lease held by another process fails every queued job after one
  timeout, not one timeout per job. A shutdown also interrupts the wait.
* The CLI and the MCP server build jobs through one module (`sim/jobs.py`) and
  validate them before submitting. `ciqsim_shot`/`ciqsim_record` now use the
  same settle (0.35 s) and budgets as `ciqkit sim shot`.

* Local screen-change detection (does a tap/key actually change anything?)
  diffs the whole frame on a fine grid instead of a face-relative region, so
  small changes (a single cell, a thin ring) are no longer missed.
* Gestures wait out slow redraws with a settle window instead of re-sending
  input, and retry only after a *verified* miss; keys are never retried (a
  repeated key press is not idempotent). The baseline frame is stable before
  the first gesture.
* Shutdown race: the daemon waits for the simulator to exit before reporting
  stopped, so the next start no longer collides with the dying process.
* Rectangular devices no longer produce false negatives in screen detection
  that assumed a round face.
* A program on port 1234 that accepts a connection and never answers hung every
  push and test run. monkeydo's port probe has no timeout of its own, so the
  "~16 s delay" reported for LM Studio was really "until the other program
  hangs up". On Windows that took more than 300 s. ciqkit's pushes skip the
  probe on Windows now too, and `simport` no longer calls the situation
  "slow, not broken".
* On Windows, killing a stale monkeydo before a push left its `shell.exe`
  running and connected to the simulator, one more per push (TerminateProcess
  takes no children). The sweep now kills the tree and any shells already
  orphaned.
* `ciqkit sim test` exited 0 whenever the job finished, so a test run with no
  verdict -- unreachable simulator, a test build that crashed on load, zero
  tests -- read as green to scripts and CI. It now exits on the tests'
  verdict, and `ciqsim_test` (MCP) reports `ok: false` for it.
* SDK-framed recordings re-loaded and flood-filled Garmin's device PNG on every
  frame (~0.3 s); it is now prepared once per device. Measured on Windows:
  2.8 fps -> 5 fps.
* Gesture confirmation missed real changes and then retried them: its 4x4
  brightness signature averaged a strong local change away (a page swipe on a
  dark app: mean 10.2 < 12, so one swipe became three pages), and could not
  see text replaced in place at all ("MINI OK" -> "HELD": no cell over 9.4).
  The signature now also carries a 16x16 grid, and two strong coarse cells or
  eight strong fine ones count as a change; one ticking digit still does not.
* The warm-load shortcut called an app "verified" when only Connect IQ's
  loader triangle was on screen (an app that had exited via BACK), and the
  loader check could not see the triangle at all when the simulator drew the
  device around the display.
* `--prg bin/App.prg` is normalised before it reaches monkeydo and the
  storage journal (on Windows it arrived as `C:\proj\bin/App.prg`).
* `CIQ_HOME=~/sdk` is normalised after expansion.

* Window grabs on macOS use Quartz first and fall back to the `screencapture`
  CLI. The CLI can hang machine-wide while Quartz answers in milliseconds; when
  it did (2026-09-28), every load failed with "pre-push baseline frame could
  not be captured".
* `sim test` reported a run that never reached the simulator as green: the
  result counted FAIL tokens, and `Unable to connect to simulator.` has none, so
  it came back `ok: true, passed: 0, failed: 0`. Its summary regex also did not
  match SDK 9.1.0's `PASSED (passed=N, failed=N , errors=N)`. It now parses that
  line, needs a non-zero `Ran N tests`, reports `errors`/`ran`, and returns a
  `reason` (`simulator-unreachable`, `no-verdict`, `zero-tests`, `timeout`)
  whenever it is not green.
* A push into a simulator that had shown its window but not bound its port
  (every restart) failed silently inside monkeydo and read as "did not load" 42 s
  later, escalating into another restart. Pushes now wait for the port. With a
  foreign listener on 1234 each monkeydo call also lost ~16 s to a probe
  timeout, a third of the load budget; the probe-skipping shim removes it.

Found by running the packaged tool end-to-end against a real project before
release, not by review:

* The round display was LOCATED by brightness even when the simulator was in
  device-art mode, where brightness is the worst available signal: outside the
  glass lies the black bezel, not bright chrome, so the chrome detector sized
  itself to the whole body and the near-black detector's "equator" moved with
  whatever the screen happened to show. On real venu3 captures that put the
  crop ~30 px above the glass on an ordinary page — clipping content drawn at
  the display rim — and on a bright one (a QR block, a large white clock) it
  lost the display entirely and the shot came back inside a second watch body.
  The glass is now MEASURED: the simulator's device art is Garmin's own SDK
  device PNG, so the body-to-screen ratio is a fixed property of the device.
  `find_display_circle` scales the PNG's screen cut-out by the body it finds in
  the capture and reads no screen pixel at all; the brightness detectors remain
  the fallback for captures that are not device art.
* A push reported "app did not load" for a screen that had loaded perfectly,
  whenever that screen was bright. The check reads pixel statistics off the
  whole simulator WINDOW, and in device-art mode most of that window is the
  sim's white canvas: a venu3 window measures 49 on an ordinary watchface and
  88 on a page with a large QR code, against a cut-off of 60. The push then
  restarted the simulator and tried again — and each restart re-ran face
  detection, which is how a single bad detection got into an otherwise good
  batch of store shots. `looks_like_app_loaded` now judges the pixels inside
  the measured display, which reads 0 on every page, bright or not.
* Store framing double-framed every LIGHT watch colorway. Device-art detection
  keyed on dark-on-white pixels, so venu2s (ivory band, pale body, gold bezel,
  all ~190 against the white canvas) showed only its black glass, came out
  round rather than strap-shaped, failed the device-art test and went down the
  SDK framer instead — which pasted the simulator's own watch inside a second
  body: a ghost bezel ring with a duplicate set of tick marks around shrunken,
  off-centre content. Detection now falls back to a canvas-relative threshold,
  dark-first so no colorway it already handled moves.
* The device-art margin walked back into the simulator's own chrome, stranding
  the title-bar rule and the toolbar glyphs across the top and bottom of the
  store shot. On a small device the watch nearly fills its window, so that gap
  is thinner than the margin. The margin now stops at the first non-canvas row
  that follows a canvas one — while still free to walk through the watch's own
  upper half when bright screen content splits it into several row runs.
* Face resolution came from a hand-written device table that went stale the
  moment an unlisted device was driven: fr265s is a 360 px face, was absent,
  and the 454 px fallback mis-snapped every crop of that run. The installed SDK
  profile (`compiler.json`) is now the source of truth, with the table kept as
  an offline fallback.
* `load` reported success on Connect IQ's own loading screen (a blue triangle
  on black). That frame is neither blank nor identical to the watchface, so
  both cheap checks passed and the captured PNG was of the loader. There is now
  an explicit loader check, and seeing the loader come and go is used as
  positive proof that the next stable frame is the app.
* Re-pushing an unchanged build produced a screen identical to the one already
  displayed, which the frame diff could not recognise as a load. Seeing the
  loader now resolves that case without the previous timeout-and-restart.
* `shot-score` reported HiDPI captures as an unknown size and as blurry.
  Retina and HiDPI captures are integer multiples of the logical size, and
  Laplacian variance is resolution-dependent; sizes at 2x and 3x are now
  recognised and sharpness is measured at logical scale.
* `shot-score` failed framed screenshots with `no_watch_body_detected` when
  OpenCV was not installed, on images that were fine. The fallback heuristic is
  weak, so without OpenCV this is now a warning that names the fix.
* Seed values were interpolated into a regular-expression replacement template,
  where backslashes are escapes: a value of `"\1"` spliced the matched text
  back into the source file and `"a\g"` raised. Values are now written verbatim.
* Seeded files were written through text mode, which encodes in whatever the
  locale happens to be even though the source was decoded as UTF-8, and which
  rewrites every newline on Windows. Seed writes are binary now.
* `detect_sdk` sorted SDK directory names as strings, so "the newest SDK" was
  really "the one whose name sorts last" — 9.9.0 beat 9.10.0. Version
  components now compare numerically.
* A screenshot on an unrecognised device silently assumed a 454 px face and
  produced an oddly cropped PNG. It now says so once, naming the fix.
* Cancelling a job whose last chunk was in flight reported it back as `done`:
  the completing chunk overwrote the terminal state. A cancelled job now keeps
  its state, and the in-flight chunk's result is discarded.
* Cancelling a job that had already seeded left the user's source patched.
  Nothing restored it until an unrelated job happened to finish, and
  `ciqkit sim restore` refuses while the broker owning the journal is alive, so
  there was no way out. Cancel now hands the restore to the scheduler thread.
* Any job finishing restored *any* active seed, so a seeded job that was merely
  paused between chunks came out unseeded on its next build. Only the job that
  applied a patch undoes it now (broker teardown still restores unconditionally).
* A chunk that blew its `max_s` left its worker thread running inside the
  SimSession while the scheduler started the next job — two threads driving the
  simulator at once, which is the one thing the architecture forbids, happening
  exactly when the simulator was already misbehaving. The timed-out worker is
  now reaped (sim restarted to unblock it, bounded wait) before anything else
  runs.
* `ciqkit sim ping` started a broker and then reported it alive, so it could
  never answer "not running". `status`, `bump`, `cancel` and `shutdown` did the
  same, booting a daemon that then idled for ten minutes. These commands, and
  the matching MCP tools, no longer autospawn.
* `--seed` combined with `--prg` patched the source, pushed the *pre-built*
  binary, restored the source, and returned a verified screenshot of the
  unseeded app. The combination is refused, on the CLI and in `ciqsim_record`.
* The broker's job table never dropped finished jobs, so it grew for as long as
  work kept arriving. Terminal jobs are now reaped after 15 minutes
  (`CIQSIM_JOB_RETENTION_S`) — long after any client could still be waiting.
* The broker printed a `sim` lease warning straight to stderr instead of
  logging it, bypassing `CIQKIT_LOG_LEVEL` and the daemon log format.
* `ciqkit sim bump` and `ciqkit sim cancel` exited 0 when the broker replied
  `no such job`, contradicting the JSON they printed. They exit 1 now, which
  matters more since finished jobs are reaped after 15 minutes.
* An OCR anchor gated results it was only meant to confirm. `act` failed every
  anchored gesture on a machine with no OCR backend, and `nav` collapsed the
  same "skipped" result to "not found" — so anchored navigation could not work
  at all without OCR installed, contrary to the documented rule that a missing
  backend degrades to a skipped check. A definite miss still fails; an
  unavailable backend no longer does.
* `ciqkit sim nav` exited 0 whether or not it reached the screen: the reach
  step's verdict was buried in the job results and never consulted. It now
  exits 3 on a contradicted anchor (matching `shot`) and 1 when a gesture did
  not land.
* `nav` discarded every gesture result, so a recipe whose taps all missed still
  reported arrival. A step that cannot be confirmed now stops the recipe and
  names `"confirm": false` as the escape hatch for gestures that are not
  supposed to change the screen.
* The `reach` chunk used the flat 120 s default however many gestures it held,
  so a long recipe timed out mid-navigation and took a simulator restart with
  it. Its budget now scales with the step count.
* `build` accepted the first `.prg` its build script *mentioned* that happened
  to exist, with no freshness check — so an `rm -f bin/App-test.prg` line or a
  warning quoting an old binary could hand back a stale or unit-test build to
  screenshot. The mtime floor that already guarded the directory scan now
  guards the announced path too, and the docstring describes what the code
  actually matches.
* `ciqkit shot-score` printed "No PNG/JPG files found." and exited **0**. Used
  as a CI gate, a renamed directory or a typo scored nothing, found nothing
  below the threshold, and reported a clean pass. It now exits 2, names the
  paths, reports mistyped files individually (they used to reach the scorer and
  raise), and keeps `--json` stdout parseable.
* `record` hitting its frame cap looked like a short clip with no explanation;
  the result now carries `truncated` and the reason is logged.
* `record` cropped every frame to a tight face box while `shot` framed the
  complete watch, so recorded GIFs clipped the top of the scene and sliced the
  watch body mid-bezel. Frames are now framed exactly like `shot` (device-art
  trim or SDK composite), with the geometry detected ONCE on the first frame
  and replayed deterministically on every frame — per-frame re-detection is
  content-dependent and wobbles on bright screens. `--native` keeps the old
  tight crop; `--no-crop` still means the raw window. The result reports
  `framed`.
* The default GIF playback pace was 120 ms per frame (~8 fps). Grabs cost ~2 s
  each at the time, so consecutive frames were distinct poses, not adjacent
  motion — played that fast the eye registers none of them. The default median
  frame duration is now `DEFAULT_PLAYBACK_MS` = 500 ms (~2 fps), defined once in
  `sim/protocol.py` and quoted by the CLI, the MCP schema, the broker fallback
  and `SimSession.record`. It is a CEILING, only ever applied to speed a clip
  up. Since the capture-rate fix below, a healthy clip's frames are ~220 ms
  apart — shorter than this 500 ms — so the track passes through at its real
  wall-clock rate and this value now only rescues a degraded capture.
* `record` sampled at ~0.5 fps (~2.1 s a frame) and the Garmin simulator got
  the blame. It was ours, in two places. `capture_window` re-probed the window
  position over AppleScript, restored it, focused it and slept a hardcoded
  1.0 s before **every** grab — ~1.85 s of window bookkeeping that the record
  loop had already done once up front, wrapped around a 0.15 s `screencapture`.
  And framing each frame ran a ~0.47 s pure-Python flood fill to repaint the
  simulator's white canvas, even though that region lies outside the bezel and
  is therefore identical on every frame of a clip. `capture_window` now takes
  `refocus` / `grab_settle_s`; `record` raises the window on frame 0 and every
  `refocus_every` (25) frames as an occlusion-and-movement guard, and computes
  the canvas once as a mask (`device_art_canvas_mask`) that it pastes per frame
  — measured pixel-identical to flooding, re-checked against the frame before
  it is trusted, and rebuilt on each re-raised frame so a transient over the
  canvas at frame 0 cannot be baked into a whole clip. Measured end to end:
  **0.5 fps → 4.6 fps**, a 12 s clip going from ~6 frames to ~50.
* Recorded GIFs froze for ~1.4 s every 25th frame and ~0.7 s after the first.
  Frame durations come from the real capture timestamps -- correct, so that a
  genuine pause on the watch reads as a pause -- but two gaps per clip measure
  ciqkit rather than the app: the canvas-mask build after frame 0 and the
  periodic window re-raise. At the old ~0.5 fps they hid inside a two-second
  frame time; at 4.6 fps they are an 8x stutter. They are now flattened to the
  median of the honest gaps, which only ever shortens them. Real stalls are
  untouched.
* `_verify_loaded`, `_safe_sig`, the warm-load check and `ensure_simulator`'s
  startup poll each paid `capture_window`'s 1 s settle on every iteration. A
  poll loop does not need it -- it is going to look again shortly, and a torn
  frame costs one more look -- so it is dropped there: ~1 s per poll, and
  `ensure_simulator`'s "faster than a blanket 2 s sleep" loop had degenerated
  into ~1 iteration per 2.5 s budget, slower than the sleep it replaced.
* `ciqsim_record` gained `force` (cold reload after a rebuild — the warm cache
  otherwise records the PREVIOUS build, same stale-warm bug `ciqsim_shot`
  already documented) and `native`; `playback_ms` is now described in terms of
  what an agent should pick.
* The contact sheet padded short clips out to a fixed six-column grid, so a
  two-frame capture rendered as two thumbnails beside four empty cells that
  looked like failed captures.

### Changed

* `--anchor` now also verifies the load, not just the captured frame — matching
  the expected text proves the *right* screen is up. The docs already described
  this; the CLI was not wiring it through.
* `ciqkit sim shot --anchor` exits **3** when OCR ran and the text was not on
  screen. It previously exited 0, so an assertion in CI silently passed. With no
  OCR backend the check is skipped rather than failed, so machines without one
  behave as before.
* `ciqkit sim shot` accepts `--seed`, which `record` already had.
* Library diagnostics go through `logging` instead of `print`, so they cannot
  contaminate the CLI's JSON on stdout or the MCP server's JSON-RPC. The daemon
  configures a handler; `CIQKIT_LOG_LEVEL=DEBUG` adds the face detector's
  per-frame reasoning.
* On a platform without POSIX uids, the private runtime directory now raises a
  named error explaining that the broker and leases are POSIX-only, instead of
  an `AttributeError` on `os.getuid` from deep in the call stack.

### Removed

* `ciqkit.store` (the screenshot store module): it belonged to the private
  toolchain this was split out of and nothing in ciqkit used it.
* 500 lines of `screenshot_utils` (21 functions) that nothing on ciqkit's path
  called — legacy from the batch-capture script this was extracted from.
  Confirmed unreachable by static analysis *and* by tracing a real simulator
  session, which produced a byte-identical screenshot afterwards.
* `ciqkit sim seed`, the standalone command. It could not do what its help and
  the docs promised: the broker restores at the end of every job, so a
  seed-only job reverted its own patch the moment it completed. Use `--seed` on
  `shot` or `record`, which seeds, builds, captures and restores as one job.
* `Job.preemptible`, a protocol field the scheduler never read. Older clients
  that still send it are accepted.

### Security

* The broker's control socket lives in a private per-user `0700` directory whose
  ownership and mode are verified before use, and the socket itself is created
  `0600`. It is a command channel — see [SECURITY.md](SECURITY.md).
* Lease files moved from a shared `/tmp` path to the same private directory,
  removing a local denial-of-service and symlink surface.
* Seed patch targets are contained to the project directory. A `file` of
  `../../.ssh/config`, an absolute path, or a symlink pointing out of the tree
  now raises instead of being written: `os.path.join(app_dir, file)` is not a
  containment check, and seeds are submittable over the control socket and by
  an MCP client.
* Seeding refuses a file with uncommitted git changes unless forced, and the
  originals are journalled to disk before any write, so a `kill -9` mid-seed is
  recoverable rather than merely detectable. Recovery is digest-guarded and
  will not overwrite a file that changed after ciqkit patched it.
* Process kills (`pkill`) are scoped to the caller's own uid, so a command-line
  pattern match cannot reach another user's process on a shared machine.

[0.1.0]: https://github.com/JohannesHoh/ciqkit/releases/tag/v0.1.0
