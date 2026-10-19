# `ciqkit sim` — the simulator broker

## The model

One background daemon owns the simulator. You submit **jobs**; it runs them one
at a time and hands back structured JSON.

A job is an ordered list of **chunks** — `load`, `shot`, `act`, `record`,
`test`, `seed`, `restore`. Chunks matter because they are the **preemption
unit**: the scheduler re-picks the best job at every chunk boundary, so a job
that arrives mid-chunk starts as soon as the current chunk finishes rather than
waiting for the whole job.

The daemon starts itself on your first command and exits after 10 minutes idle.
You never start it by hand.

## Priorities

| Priority | Use for |
| --- | --- |
| `urgent` | "I want this screenshot now" — preempts everything preemptible |
| `high` | interactive capture or navigation |
| `normal` | the default |
| `batch` | long test runs and bulk capture; yields to everything above |

```bash
ciqkit sim test --priority batch                            # long run; builds its own -t variant
ciqkit sim shot now --urgent                                # jumps ahead
ciqkit sim status                                           # see the queue
ciqkit sim bump <job_id> --priority urgent                  # promote after the fact
ciqkit sim wait <job_id>                                    # re-attach and collect
```

`wait` matters because the command that submitted a job gives up before the job
does. A queued build behind a long test run can outlast the default wait; the
command prints `wait timeout` and exits 1, but the job is still running and will
still write its PNG. `ciqkit sim wait <job_id>` picks it back up, with the same
exit codes — including exit 3 for a contradicted anchor.

`cancel` stops a job at its next chunk boundary (the chunk already in flight
runs to completion — that is the only preemption point) and puts back anything
its seed patched.

"Preempts everything preemptible" has one exception: while a job's seed is
patched into a project's source, another job that would **build that project**
(or seed anything) waits until the seed is restored — otherwise it would
compile the other job's `SEED_*` values into its own screenshot. A pre-built
`--prg`, or a different project, still preempts as usual.

`ciqkit sim shutdown` fails queued jobs straight away with "broker is shutting
down", lets the chunk in flight finish (up to `CIQSIM_SHUTDOWN_GRACE_S`, 20 s),
then exits. SIGTERM/SIGINT do the same; a second signal exits immediately. If
the broker's `sim` lease cannot be taken because another process
holds the simulator, every queued job fails at once, naming the holder.

## Navigation

`ciqkit sim nav` runs its gestures and then verifies it arrived, and its exit
code says which:

| Exit | Meaning |
| --- | --- |
| 0 | every gesture landed, and the anchor matched or was skipped |
| 1 | a gesture did not change the screen (or the job itself failed) |
| 3 | the anchor was checked and the expected text was not there |

A gesture that is not supposed to change the screen would otherwise stop the
recipe; pass `"confirm": false` on that step.

The whole recipe runs as **one** chunk. Nav is stateful: preempting between
gestures would let another job load a different app, and a reload lands on the
app's first screen with the remaining gestures aimed at somewhere they were
never meant to go. The chunk's budget scales with the number of steps.

A waiting job ages toward more-urgent (one tier per 45 s), so a stream of
urgent work cannot starve a batch job forever.

When an urgent job displaces a running one, the displaced job goes to `paused`
and resumes at its next chunk. If the intervening job pushed another app (or
another build of this one), it instead rewinds to its own load — rebuilding,
not re-pushing a shared output file — and replays the gestures after it, so it
is back on the screen it had reached before it shoots.

## Load verification

The failure this exists to prevent: `monkeydo` returns immediately, so a naive
script screenshots whatever was on screen before — usually the device watchface
— and the PNG looks plausible enough that nobody checks.

The near miss it also prevents: Connect IQ shows a blue triangle on black
while an app loads. That frame is not blank *and* has changed from the
watchface, so both cheap checks pass on it — and you get a screenshot of the
loader.

A frame is accepted as the loaded app only if **both** of these hold:

* it is **not blank**, and
* it is **not the loading screen** — an almost-black face carrying a small,
  strongly blue-dominant mark and nothing else.

and then **either** of these:

* the **loader was seen to come and go** since the push. Connect IQ only shows
  it while loading, so having watched it appear and clear is positive proof
  that this frame is the app that just finished loading. This is what makes a
  re-push work when the new screen is pixel-identical to the one already up;
* the **face differs from the pre-push frame** (a brightness signature, so it
  works on a white QR page and a dark menu alike).

If no pre-push frame could be captured, any non-blank, non-loader frame is
accepted — there is nothing to compare against.

**`--anchor` is a fourth, stronger signal, and the only one that identifies
*which* screen you are looking at.** When OCR finds the text, the load is
confirmed immediately (`anchor_confirmed: true`). When it does not — small
watch fonts do miss — the load is still accepted once the frame is stable and
the result reports `anchor_confirmed: false`. The anchor strengthens
confidence; it does not gate the load. With no OCR backend installed, anchors
are skipped entirely.

Failure escalates: re-push, then kill and relaunch the simulator, up to three
attempts.

## Commands

### `shot`

```bash
ciqkit sim shot NAME [--project DIR] [--prg PATH] [--device venu3]
                     [--anchor TEXT ...] [--native] [--no-crop] [--force]
                     [--out-dir DIR] [--urgent | --priority P]
```

`--device` defaults to the first `<iq:product>` in `manifest.xml` that is
installed in the SDK (else `venu3`).

Pass `--force` after rebuilding the same `.prg`. The broker keeps the app
"warm" and skips a re-push when the same project is already loaded, which is
what makes a repeat capture take 5 s instead of 80 — but after a rebuild the
output path is unchanged, so the warm shortcut would re-shoot the *previous*
build's screen.

Default output is the watch face composited onto the device body and strap from
your SDK's device images — what a store listing image should look like. Pass
`--native` for the bare cropped face, `--no-crop` for the whole simulator
window.

Without `--out-dir`, PNGs go to a private runtime directory (printed in the
result). ciqkit never writes into your project unless you ask it to.

`--anchor` is used twice: to verify the load (matching it proves the *right*
screen is up), and again on the captured frame. Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | captured; any anchor given was found, or OCR was unavailable |
| 1 | the job failed — the app never loaded, the build failed, and so on |
| 3 | captured, but OCR ran and the anchor text was **not** on screen |

Exit 3 is what makes `--anchor` usable as an assertion in CI. It cannot fire
without an OCR backend: with none installed the check is skipped, not failed,
so install `ciqkit[ocr]` if you want the assertion to mean anything.

`--on-marker TEXT` takes the shot when the app prints `TEXT` on its console
instead of right after the load; `--fit FILE` plays a FIT file into the app.
Both are described under [Console markers](#console-markers-shot-on) and
[FIT playback](#fit-playback-on-load).

### `record`

```bash
ciqkit sim record NAME --duration 8 [--interval 200] [--seed NAME=VALUE]
```

Writes `NAME.gif`, `NAME.sheet.png` (a numbered contact sheet for frame-by-frame
review) and `NAME.frames/`.

**This is a periodic sampler, not a screen recorder.** Every frame is a separate
window grab, measured **~0.22 s per frame (≈4.6 fps)** on one macOS machine, so
`--interval` is a floor the capture rate may not reach: a 12 s clip produced
~50 frames.

It used to be ~2.1 s per frame (≈0.5 fps), and the simulator got the blame. It
was ours: `capture_window` re-probed the window position, restored it, focused
it and slept 1 s before *every* grab (~1.85 s), and framing each frame ran a
~0.47 s flood fill to repaint the simulator's static white canvas. The record
loop now raises the window once (and every 25th frame, as an occlusion guard)
and computes the canvas as a mask once per clip. The remaining ~0.15 s is
`screencapture` itself.

On Windows a grab is ~0.04 s (`PrintWindow`), so a clip runs at the
`--interval` floor: 5 fps at the default 200 ms. The Windows simulator window
never shows the whole device art, so frames are framed onto Garmin's device
PNG; that PNG is prepared once per device, not per frame.

Use it to confirm that something changes over time, to step through states, or
to watch an animation at ~4.6 fps. It is still sampling: anything faster than
that aliases. Frame durations come from the *actual* capture timestamps, and
`--playback-ms` is a ceiling that only ever speeds a clip up — a clip captured
at the normal rate plays at its real speed and is left alone.

A clip is capped at 300 frames. If the cap is reached before `--duration`
elapses the result sets `"truncated": true` and the broker log says so, rather
than quietly handing back a short clip.

### `nav`

```bash
ciqkit sim nav --to settings \
  --steps '[{"kind":"tap","x":0.5,"y":0.85},{"kind":"swipe","direction":"up"}]' \
  --anchor "SETTINGS"
```

Replays the gestures you supply, then verifies the anchor. Each gesture is
individually confirmed against a baseline frame the screen has settled on
(up to 2 s of waiting for two consecutive frames to agree, since an app that
has just loaded may still be drawing). The screen must then change, or the
step's own anchor appear, within a settle window of about 2.5 s; it is polled,
so a slow redraw is waited for rather than mistaken for a dropped input.
"Changed" means either a large change across the whole face or a strong change
in a few small regions of the simulator window, so a local update such as one
mark placed on a dark game board counts. The flip side: anything else that
moves in the window during the settle window -- an animation or a ticking
clock in your app, a notification banner over the simulator -- also reads as
"changed". Give a step an `anchor` where that matters.

Only an input that showed no effect for the whole settle window is re-sent, up
to twice (`"retries": N` on a step changes that, 0–3; the broker log notes
every retry). Inputs do get dropped -- most often the first key after a cold
load -- and because a retry only follows a full window with no visible effect,
a START press is not sent twice unless the app took longer than that window to
react to the first one. A failure is reported as `step N of M` (1-based).

`kind` is `tap` (with `x`/`y` as 0–1 fractions of the screen), `hold` (the
same, held ~1.6 s), `swipe` (with `direction`), `key` (with `key`: `enter`
is START, `esc` is BACK; arrows only on devices with UP/DOWN buttons), or
`button` (with `id`, see below).

`button` presses one of the device's physical keys by clicking its spot on the
simulator's device art: `{"kind":"button","id":"enter"}`. The ids are the `keys`
of the device's `simulator.json` (`enter`, `esc`, `up`, `down`, `menu`, ... --
whatever that device lists; an unknown id or a device with no keys fails with
the list of available ids). A key defined as a hold in the profile (`menu` on
most devices, which shares a spot with `up`) is pressed ~1.6 s; `"hold": true`
or `false` overrides that. It is retried and confirmed like any other step.
The click is aimed by sliding the device PNG's silhouette over the capture, so
it needs the device art on screen at its normal size.

On macOS a Return or Esc *keystroke* is not delivered to the app as START or
BACK (arrow keys are), so a `key` step with `enter`/`esc` is routed through the
`button` click whenever the device defines that key, and sent as a keystroke
otherwise. Windows keeps its keystroke path, which works.

On Windows the fractions are of the watch display itself, located on the
capture just before each gesture: the simulator there always draws the device
body around the display, so fractions of the whole view would land on the
bezel. Taps and swipes then also depend on the simulator's own Touch Screen
setting; see [Known limits](#known-limits).

This is a gesture replayer, not a UI-tree driver. It cannot find a button by
name.

#### Explicit and edge swipes

A plain `{"kind":"swipe","direction":"left"}` is the default flick: centred on
the display, about 0.42 of its width, so both ends stay mid-face. Give it
geometry when the start matters -- a swipe that must begin at the edge (a
side-swipe menu, a back gesture):

```json
{"kind":"swipe","direction":"left","from":[0.97,0.5],"dist":0.4}
{"kind":"swipe","edge":"right"}
```

`from` is the start point as `[x, y]` display fractions (0..1) and `dist` the
drag length as a fraction of the display width (default 0.4 when `from` or
`edge` is given); the drag runs from `from` toward `direction`. `edge`
(`left`, `right`, `top`, `bottom`) is shorthand for a start at that edge
(0.03 / 0.97) pulling inward: `edge: "right"` is `from [0.97,0.5]`,
`direction: "left"`. An explicit `from`, `dist` or `direction` overrides the
shorthand. Fractions are of the display on macOS and Windows alike. The same
fields work on a lone ACT chunk and through `ciqsim_nav`.

### FIT playback on load

```bash
ciqkit sim shot run --prg bin/App.prg --fit recordings/climb.fit
ciqkit sim nav --to timer --fit climb.fit --steps '[...]'     # also: record
```

After the verified load, `--fit FILE` (`"fit"` on the LOAD chunk, the
`ciqsim_shot` / `ciqsim_nav` argument) drives the simulator's own player
(Simulation > Activity Data): it picks *FIT/GPX Playable File*, loads the file
through the file panel, presses play, then **verifies** playback. The window
must show the requested file and its playback clock must have advanced; if
not, the load fails with `FIT playback did not start: <reason>` rather than
handing back a shot of an app that is not being fed. The result carries
`fit: {file, total_s, elapsed_s}`. The Activity Data window is closed again
afterwards so it does not sit over the watch face. As a nav step it is
`{"kind":"fit","path":"/abs/climb.fit"}`. macOS only (Windows reports it as
unsupported). It is the simulator that interprets the file: `monkeydo` takes no
FIT argument (a third positional is ignored with "ILLEGAL ARGUMENT").

### Console markers: `shot-on`

An app's own `System.println("MX_SHOT page1")` is a better trigger than a
guessed sleep. monkeydo streams the running app's console to its stdout; the
broker keeps that in a file (`console.log` beside the screenshots) and a
**marker wait** reads it with a cursor that starts at the push and only moves
forward, so a marker printed while an earlier shot was being taken is found
immediately, and the same text printed twice is two hits, in order.

```bash
ciqkit sim shot page1 --on-marker "MX_SHOT page1" --marker-timeout 20
ciqkit sim shot-on --marker "MX_SHOT page1=p1" --marker "MX_SHOT page2=p2"
```

`shot-on` is one cold load and then one shot per `--marker TEXT=NAME`
(`NAME.png`), in the order the app prints them; the marker is a substring of
the line. As a nav step: `{"kind":"shot_on","marker":"MX_SHOT page1","name":"p1",
"timeout":20}` (optionally `regex: true`, `out_dir`, `frame`, `crop`, `anchor`);
a marker that never appears fails the step and the error includes the console's
last lines. The shot's normal settle (0.35 s) counts from the marker. Markers are
read from the app pushed last, so a marker shot is always a cold load -- a warm
load pushes nothing and there would be no console to read. Only lines with a
newline count, so a half-written line is never matched.

### Simulator ops: `phone`, `close-aux`

```bash
ciqkit sim phone disconnected     # Settings > Connection Type > BLE > Not Connected
ciqkit sim phone connected
ciqkit sim close-aux
```

Both are also nav steps: `{"kind":"phone","state":"connected"|"disconnected"}` and
`{"kind":"close_aux"}`. `phone` clicks the simulator's menu, reads it back and
reports `state` and `phoneConnected` (`ok` is false if the menu does not show what
was asked). `DeviceSettings.phoneConnected` in the app follows this menu: not
connected gives `false`. macOS only. The setting persists in the simulator, so a
capture recipe that depends on it should set it explicitly.

Auxiliary windows: `Communications.openWebPage` makes the simulator open a
non-modal "Webpage Window" (a real web view). Before every gesture and shot ciqkit
closes every simulator window except the device window (titled `CIQ Simulator -
<device>`), and the device window is raised, so a shot never captures a web view;
`close-aux` does it on demand. Set `CIQKIT_KEEP_AUX=1` to leave them open.

Crashes: if the simulator crashes, macOS shows "Connect IQ Device Simulator quit
unexpectedly" (UserNotificationCenter: Reopen / Ignore / Report...). ciqkit clicks
**Ignore** and records it: the step or shot result gets a `dismissed` list and
`"simulator_crashed": true`. Crash logs are in `~/Library/Logs/DiagnosticReports/
simulator-*.ips`. Gesture `x`/`y` are fractions of the watch display on macOS too
(the window draws the whole watch body around it).

`button` steps take `id` (canonical) or `button` (alias), in nav recipes and ACT alike.

### `test`

```bash
ciqkit sim test --priority batch                  # builds the -t variant itself
ciqkit sim test --prg path/to/MyApp-test.prg      # or run a PRG you built
```

Runs `monkeydo <prg> <device> -t` and parses the harness summary into
`{ok, passed, failed, errors, ran, out}`, plus `reason` when it is not green.

`ok` needs a `PASSED` verdict **and** a non-zero `Ran N tests`. monkeydo's exit
code is 1 even on a clean pass, so it is ignored. What is never green:

| `reason` | Meaning |
|---|---|
| `simulator-unreachable` | No simulator answered on 1234–1238 (see [the port](#the-simulator-port)); `detail` says why. |
| `no-verdict` | The harness printed no summary. On SDK 9.1.0 a PRG with zero test cases prints *nothing*, so this is usually the next row. |
| `zero-tests` | `Ran 0 tests`. The build had no test cases: made without `monkeyc -t`, or from a jungle with `base.excludeAnnotations = test`, which strips `(:test)` even under `-t`. |
| `timeout` | The run exceeded its budget. |

`--prg` is optional. Omitted, the broker builds the unit-test variant inside the
job with the built-in `monkeyc ... -t` build (output `bin/<name>-test.prg`). A
project with a custom build command (`CIQKIT_BUILD_CMD`, `scripts/build.sh`) has
no known test variant, so the job fails with a message asking for `--prg` (or set
`[build] test_cmd` in [`ciqkit.toml`](config.md)). Either
way the build must come from a jungle that does not exclude the `test`
annotation.

### Named screens

`ciqkit sim shots --all` / `--screen NAME` regenerate the `[[screens]]` saved in
`ciqkit.toml`; see [config.md](config.md).

### `--seed`

```bash
ciqkit sim shot   demo    --seed SEED_DEMO_DATA=true --seed SEED_START_PAGE=2
ciqkit sim record splash  --seed SEED_FORCE_INTRO=true
```

Patches `const NAME = VALUE;` in `source/Constants.mc` so you can reach a screen
that normally needs a finished session or a menu path. Seeding, building,
capturing and restoring happen inside **one job**, so the patch exists only for
as long as that job runs. There is no separate `seed` command: seeding that
outlives its job would be a patch nobody is accountable for putting back.

Restore is byte-exact, from a backup taken before the edit, and runs on job
completion, on failure, on `SIGTERM`/`SIGINT` and from an `atexit` hook. The
result reports whether `git` agrees the file is clean again.

Two things it will refuse, both because this writes into your working tree:

* **a target outside the project.** Seed paths are resolved against the project
  directory and must stay inside it — an absolute path, a `../` escape, or a
  symlink pointing out is rejected. The broker socket and the MCP server can
  both submit seeds, and neither should be able to rewrite arbitrary files.
* **a file with uncommitted changes.** Restore is reliable, but `SIGKILL`
  cannot be caught, and `git checkout` only rescues work you had committed.
  Commit or stash first.

If the broker *is* `kill -9`'d mid-seed, the originals are still on disk: they
are journalled to ciqkit's private runtime directory before the first byte is
written. The next broker start puts them back and says so, or run:

```bash
ciqkit sim restore
```

Recovery will not overwrite a file that changed after ciqkit patched it — if
you already fixed it by hand, or a checkout restored it, undoing our patch would
throw your version away. Those files are left alone and reported, with the path
to the original bytes, by `ciqkit sim restore` and `ciqkit doctor`.

## Using it as a library

```python
from ciqkit.sim.client import Client
from ciqkit.sim.protocol import Job, Chunk, ChunkKind, Priority

c = Client()
job = Job(app="my-app", priority=Priority.HIGH, chunks=[
    Chunk(ChunkKind.LOAD, {"app_dir": "/path/to/project", "device": "venu3"},
          expected_s=40, max_s=300),
    Chunk(ChunkKind.SHOT, {"name": "home", "anchor": ["TOTAL"]}),
])
result = c.submit_wait(job)   # waits for the job's own budget + queue slack
```

The daemon auto-spawns on first connect.

## From an MCP client

```jsonc
{ "mcpServers": { "ciqsim": { "command": "ciqkit-mcp" } } }
```

Tools: `ciqsim_status`, `ciqsim_shot`, `ciqsim_record`, `ciqsim_nav`,
`ciqsim_test`, `ciqsim_doctor`, `ciqsim_bump`, `ciqsim_cancel`. They take the
same arguments as the CLI, including `project`, `out_dir` and `force`; `device`
defaults to the first manifest product installed in the SDK, like the CLI.

* `ciqsim_nav {to, steps, anchor?, prg?, ...}` is `ciqkit sim nav`. Its `ok` is
  the reach verdict: false when a gesture did not land or the anchor text was
  contradicted, even though the job completed.
* `ciqsim_shots {screens?|all?, ...}` is `ciqkit sim shots`: one job per
  `[[screens]]` entry of `ciqkit.toml`; returns `{ok, screens: [...]}`.
* `ciqsim_test {prg?}` is `ciqkit sim test`. Without `prg` the broker builds the
  unit-test variant (`monkeyc -t`, written to `bin/<name>-test.prg`) inside the
  job. A project with a custom build command (`CIQKIT_BUILD_CMD`,
  `scripts/build.sh`) has no known test variant and must pass `prg`.
* `ciqsim_doctor {project?}` is `ciqkit doctor [--project DIR]` as structured
  data: `{ok, checks: [{name, status, detail, notes}]}`, status one of
  `ok`/`warn`/`fail`/`info`. It never starts the broker. With `project` it also
  checks the manifest, installed devices, developer key, build route,
  `monkey.jungle` and `(:test)` discoverability.

Read [SECURITY.md](../SECURITY.md) first — these tools can build any project the
client names.

## The simulator port

`monkeydo` has no port option. On SDK 9.1.0 (`ShellUtils.findSimulatorPort` in
`monkeybrains.jar`) it walks 127.0.0.1:1234–1238, spawns `bin/shell` against each
port, and takes the first one whose shell prints `Shell Version`. The simulator
binds the first free port from 1234 upward, and it has no port setting either:
no flag, environment variable or `simulator.ini` key.

So the range cannot be moved, and nothing in ciqkit tries to move it. Two
consequences, both handled by `ciqkit.sim.simport`:

* **Another program on 1234** (LM Studio's local server is the common one) sits
  in front of a simulator on 1235. That server accepts the TCP connection but
  never speaks the device protocol, and monkeydo's probe has no timeout of its
  own: it waits until the shell gives up. Against LM Studio `bin/shell` gives up
  after about 16 s (`Timeout waiting for device info`) on every call; against a
  program that accepts and never answers, it never does, and the push hangs
  (measured on Windows: still waiting after 300 s). ciqkit removes the probe by
  handing monkeydo its own `-s` shell: a shim that passes the simulator's real
  port to the SDK shell and refuses every other port at once. The shim engages
  only when a foreign listener is actually in the way, and falls back to plain
  `monkeydo` if java, the SDK shell or the `MonkeyDoDeux` class is missing.
  On POSIX it is a two-line `sh` script that `exec`s the real shell. On Windows
  it is a batch file. A batch file cannot `exec`, and monkeydo ends each shell
  with `Process.destroy()`, which kills `cmd.exe` but not its child. The orphaned
  `shell.exe` kept monkeydo's pipe open, so a run that had printed `PASSED`
  never exited. So the simulator's port goes through `shellrelay.py`, which
  kills `shell.exe` when its `cmd.exe` dies.
* **On Windows a program can also bind `127.0.0.1` on the simulator's own port**,
  which the simulator holds on `0.0.0.0`. Windows allows that without any socket
  option, and the more specific bind gets monkeydo's connection. `simport`
  reports this as unreachable and names the program.
* **On Windows ciqkit runs monkeydo's java line itself** rather than
  `bin\monkeydo.bat`: the batch file only accepts `/t` (not `-t`), so a test run
  passed it the POSIX flag and never reached the simulator, and it re-parses
  the `.prg` path through `cmd.exe` quoting. Same jar, class and `shell.exe`.
* **`Unable to connect to simulator.`** means none of the five ports answered:
  the simulator is not running (it crashes; on macOS see
  `~/Library/Logs/DiagnosticReports/simulator-*.ips`), has shown its window but
  not bound its port yet (every restart), or ended up past 1238 because five
  ports were taken. Pushes and test runs wait up to 20 s for the port first.

```bash
python -m ciqkit.sim.simport            # where is it, and what is in the way; exit 3 if unreachable
python -m ciqkit.sim.simport --start    # launch it if it is not running, then wait for the port
python -m ciqkit.sim.simport exec-monkeydo --sdk "$SDK" -- app.prg venu3 -t
```

## App storage: journal, start state, crash evidence

The simulator keeps every installed app's persistent state in one shared
folder:

```
$TMPDIR/com.garmin.connectiq/GARMIN/APPS/MEDIA/<NAME>.PRG             installed app
$TMPDIR/com.garmin.connectiq/GARMIN/APPS/DATA/<NAME>.DAT|.IDX|.IMT   Application.Storage
$TMPDIR/com.garmin.connectiq/GARMIN/APPS/DATA/MEDIA/OBJSTORE/<NAME>/  complications etc.
$TMPDIR/com.garmin.connectiq/GARMIN/APPS/SETTINGS/<NAME>.SET         Properties
$TMPDIR/com.garmin.connectiq/GARMIN/APPS/LOGS/CIQ_LOG.YML             crash log (Filename: <NAME>)
```

**`<NAME>` follows the app UUID, not the PRG you pushed.** A push behaves like
an update on a watch: when an app with the same UUID is installed, the new PRG
replaces it under the installed name and inherits its storage (measured on
SDK 9.1.0: a copy of `stprobe.prg` pushed as `stprobe2.prg` read and wrote
`STPROBE`'s storage). So every build of one app -- debug, capture, scratch
trees, and its `-t` unit-test PRG -- shares one storage file named after
whichever was installed first, and a run boots on what the last one left,
tests' deliberate junk included. The simulator re-reads the files at every app
start, and replacing a running app does not write its state back.

ciqkit learns `UUID -> installed name` after every push (the push rewrites
`MEDIA/<NAME>.PRG`; the UUID is read from the PRG's entry-point section) and
keeps it in `installed.json` in the journal. Before a push it resets the
learned name; if the push turns out to have landed elsewhere, `load()` resets
that storage too and pushes once more.

* Every cold `load` and every `test` run first **journals** that app's storage
  files plus an entry (app, PRG + sha256, run kind, time, git sha/dirty of the
  PRG's checkout, a mark of CIQ_LOG.YML). The journal keeps the last 20 per
  storage name (`CIQKIT_STORAGE_JOURNAL_KEEP`) under
  `~/.local/state/ciqkit/storage-journal` (`CIQKIT_STORAGE_JOURNAL`) --
  persistent on purpose, unlike `$TMPDIR`.
* The run then starts from a **declared state**: `storage_start="fresh"`
  (empty; the default), `"keep"` (inherit), or a fixture directory laid out
  like a snapshot (`DATA/*.DAT|IDX|IMT`, `SETTINGS/*.SET`, one app). Fixture
  files are renamed to the PRG's own storage name.
* If CIQ_LOG.YML gains an entry for the app, or a caller reports a crash
  screen, the snapshot is **pinned** next to the run's artifacts as
  `sim-storage-<id>/` -- `pre-run/`, `post-run/`, `CIQ_LOG.YML`,
  `crash-excerpt.yml`, the PRG, `PIN.json` -- and exempt from rotation.

```bash
ciqkit sim shot NAME --storage keep|fresh|DIR   # start state of the cold push
ciqkit storage where                     # sim dir, CIQ_LOG, journal
ciqkit storage list [PRG|NAME]           # journaled runs, newest last
ciqkit storage snap PRG --kind capture --start fresh   # prints the id
ciqkit storage check ID [--pin-to DIR]   # exit 3 + pin on a crash
ciqkit storage restore ID|DIR|NAME@latest [--as other.prg]   # replay
ciqkit storage reset PRG [--fixture DIR]
```

`restore` journals what it replaces first, so it is never lossy. Only one
storage name's files are ever read or written. Reset before the push, with the
app not running.

## Known limits

* Capture on macOS is a screen-region grab: the simulator window must stay
  unobscured. (Windows reads the window's own drawing; covering it is fine.)
* An app whose first screen is pixel-identical to the watchface behind it
  cannot be told apart by the frame diff.
* **macOS: a cold push of an unchanged build costs one simulator restart.** If the
  simulator is already showing exactly the screen the push will produce, and
  the broker has no memory of having loaded it (a fresh daemon), the frame
  never changes and Connect IQ does not always re-show its loading screen, so
  the two signals are exhausted. The broker escalates to a restart and gets it
  right on the second attempt — correct, but roughly 40 s slower.

  Measured on one project: first capture after a broker restart 87 s (two
  attempts); the next capture 5.7 s (warm path). Leaving the daemon running
  between captures — the normal case, since it starts itself and exits after
  10 minutes idle — avoids this entirely.

  Windows does not have this limit: the simulator destroys and re-creates its
  device view on every push, unchanged build included, so the broker sees the
  push land (`"verified": "push-seen"`). Measured: an unchanged re-push
  verified in ~6 s end to end.
* Windows: gestures need an unlocked desktop. A locked session still captures
  (shots, recordings, loads and test runs all work), but Windows discards
  synthesized input, so a tap, swipe or key fails at once with "the Windows
  session is locked" instead of being retried.
* Windows: whether taps and swipes reach the app at all is the simulator's own
  **Settings → Touch Screen** switch (`TouchScreenEnabled` in
  `%APPDATA%\Garmin\ConnectIQ\simulator.ini`). `ciqkit doctor` warns when it is
  off; the Enter and Esc keystrokes (START, BACK) work regardless.
* One device at a time. There is one simulator.
* `restart()` kills and relaunches the simulator; anything the app had in
  memory is gone.
