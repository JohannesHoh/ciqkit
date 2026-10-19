# CLAUDE.md

Guidance for AI agents (and people) working on this Connect IQ watch app.
README.md explains what the app is; this file is how to change it safely.

## What this is

Tic-tac-toe against the watch, in Monkey C. Targets `venu3` (round, touch),
`fr255` (round, buttons only) and `venusq2` (rectangular, touch). Every change
has to work on all three, with touch **and** with buttons.

## Commands

Run from this directory. ciqkit builds the app itself inside each `sim` job
(there is no build script), so there is no separate build step.

```bash
ciqkit doctor --project .                          # SDK, devices, key, build, tests
ciqkit sim test                                    # unit tests in source/tests/
ciqkit sim test --device fr255
ciqkit sim shot NAME --native --anchor TEXT        # screenshot + OCR check
ciqkit sim shots --all --native --out-dir shots    # every screen in ciqkit.toml
ciqkit sim shots --all --native --device fr255 --out-dir shots/fr255
ciqkit layout --round shots/*.png                  # --rect for venusq2
```

With the MCP servers in `.mcp.json` the same is available as `ciqsim_test`,
`ciqsim_shot`, `ciqsim_shots`, `ciqsim_nav` and `ciqsim_doctor`.

## Verify every UI change

A build that compiles is not a screen that looks right. After changing anything
that is drawn or any input handling:

1. `ciqkit sim test` still passes.
2. Regenerate the screens on **each** device (`sim shots --all --native
   --device D`) and look at the PNGs. Use `--anchor` with text that must be
   on the screen; an anchor that is not found means you are not on the screen
   you think you are.
3. `ciqkit layout` on the results: nothing clipped by the round bezel.
4. If you added a screen, add a `[[screens]]` entry for it in `ciqkit.toml`.

Never commit a framed screenshot (one taken without `--native`): it contains
the device image from Garmin's SDK.

## Look APIs up, do not guess them

Monkey C APIs differ by API level and device. Use the `garmin-sdk` MCP server:
`garmin_lookup_api` (e.g. `Toybox.Graphics.Dc`), `garmin_search_sdk`,
`garmin_get_device_info` (buttons, touch, shape per device). Before writing
input, storage or drawing code, read the pitfalls with `garmin_get_gotchas`;
the ones that matter most here:

* `me` is a reserved word; so are a few other short names. Rename, do not fight.
* Strings compare with `.equals()`, not `==`.
* Every callback has a watchdog: no long loops in `onUpdate` or input handlers.
  This is why `Ai.chooseMove` answers the empty board without searching.
* `BehaviorDelegate` turns a tap into `onSelect` first. `GameDelegate`
  deliberately does not override `onSelect`, so taps reach `onTap` with their
  coordinates and the START button arrives in `onKey`.
* Use `Application.Storage`, not `Application.Properties`, for runtime data.

## Code map

* `source/Constants.mc` — tunables. Each must stay a one-line
  `const NAME = VALUE;` so `--seed NAME=VALUE` can patch it.
* `source/Board.mc`, `source/Ai.mc` — pure logic, no UI. Put new rules here
  and cover them in `source/tests/`.
* `source/Game.mc` — the state machine (home, playing, result). Input goes
  through it; the view only reads it.
* `source/GameView.mc` — all drawing. Positions are fractions of the screen;
  the grid sits inside the round face's inscribed square.
* `source/GameDelegate.mc` — input mapping for buttons, taps and swipes.

## Tests

`(:test)` functions in `source/tests/` are compiled only by `monkeyc -t`, which
`ciqkit sim test` does for you. Helpers that are not tests go in a `(:test)`
class (see `Pic` in `BoardTest.mc`): the runner calls every `(:test)` function
as a test. Do not add `base.excludeAnnotations = test` to `monkey.jungle`.
