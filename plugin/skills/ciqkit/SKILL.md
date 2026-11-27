---
name: ciqkit
description: Test a Garmin Connect IQ (Monkey C) watch app, watch face or data field in the Connect IQ Simulator with ciqkit — verified screenshots, navigation by gestures, unit tests, and checks for content clipped by a round bezel. Load when changing anything a Connect IQ app draws or how it handles input, when asked to screenshot, test or check a watch app, or when the ciqsim_* tools are available.
---

# Testing a Connect IQ app with ciqkit

ciqkit (unofficial, not affiliated with Garmin) runs a small broker in front of
the Connect IQ Simulator. You submit jobs; it builds, pushes, **proves the app
loaded**, captures or gestures, and returns JSON. Several callers (you, scripts,
CI, the developer) share one simulator through its queue, so never drive the
simulator yourself with `monkeydo`, `connectiq` or screen-capture tools.

## Setup check

* `ciqkit-mcp` on PATH and the `ciqsim_*` tools listed → ready. Otherwise the
  developer installs
  `pip install "ciqkit[sim,ocr] @ git+https://github.com/JohannesHoh/ciqkit"`
  (Python 3.11+; macOS or Windows; on Linux only the static checks work).
* First call in a session: `ciqsim_doctor {project: "."}` (or
  `ciqkit doctor --project .`). It reports SDK, installed devices, developer
  key, build route and test discoverability, and never starts the broker. Fix
  `fail` rows before anything else; macOS needs Screen Recording and
  Accessibility granted to the broker.
* No `ciqkit.toml` yet: `ciqkit init` shows what it would add; it writes only
  with `--write`. Ask the developer before running that.

## The loop

1. Change the code.
2. Unit tests: `ciqsim_test` (CLI: `ciqkit sim test`). It builds the `-t`
   variant itself. `ok` needs a PASSED verdict **and** at least one test ran;
   `reason: zero-tests` usually means `base.excludeAnnotations = test` in
   `monkey.jungle` — that strips `(:test)` even under `-t`.
3. Screens: `ciqsim_shot {name, anchor: "TEXT THAT MUST BE VISIBLE"}`, or
   `ciqsim_shots {all: true}` for every `[[screens]]` in `ciqkit.toml`.
4. **Look at the PNG.** A build that compiles is not a screen that looks right.
5. Layout: `ciqkit layout <dir>` (add `--rect` on rectangular devices) flags
   text clipped by a round bezel or off screen.
6. Fix and repeat.

## Reading results honestly

* `ok: false` means it did not happen. Do not report a screenshot, a passing
  test or a reached screen that a result did not confirm.
* Shot exit/verdict: 0 captured and anchor found (or OCR unavailable — then the
  anchor was **skipped**, not passed: say so); 1 job failed (build, load);
  3 captured but the anchor text was **not** on screen — you are not on the
  screen you think you are.
* `anchor_confirmed: false` / skipped is not a failure; small fonts defeat OCR.
  Look at the image instead.
* Read `warnings` (memory pressure, a window covering the simulator).

## Navigation

`ciqsim_nav {to, steps, anchor}` replays gestures and checks each changed the
screen. Aim in **fractions of the display (0–1), never pixels**:

```json
[{"kind": "tap", "x": 0.5, "y": 0.85}, {"kind": "swipe", "direction": "up"},
 {"kind": "key", "key": "enter"}]
```

`kind`: `tap`, `hold`, `swipe` (`direction`), `key`. A step that should not
change the screen needs `"confirm": false`. A failure names `step N of M`.
Animations or a ticking clock read as "changed": give such steps an `anchor`.
On Windows a locked session drops input; gestures then fail outright.

## Don'ts

* Never commit a framed screenshot (`--framed`): it contains Garmin's device
  image. Never commit a developer key, `.prg`, `.iq` or SDK files.
* Do not guess Connect IQ APIs. If the `garmin-sdk` MCP server is present, use
  `garmin_lookup_api` / `garmin_get_gotchas`; otherwise read the SDK docs
  installed on this machine.
* Do not loop on a failing load: run `ciqsim_doctor` and report what it says.
* `ciqsim_status` / `ciqsim_cancel` for jobs you submitted; `ciqsim_bump` only
  when the developer asks to reprioritise.

More: https://github.com/JohannesHoh/ciqkit — `docs/sim.md` (every op and exit
code), `docs/checks.md`, `docs/adopt.md`.
