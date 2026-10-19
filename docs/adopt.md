# Adopting ciqkit in an existing project

For a Connect IQ project you already have (a directory with `manifest.xml`).
Starting from nothing? Use the [tutorial](tutorial/README.md) instead. Install
and permissions first: [install.md](install.md).

## 1. Doctor

```bash
cd my-project
ciqkit doctor --project .
```

Checks the environment and the project: manifest, which listed devices are
installed, developer key (`ciqkit keygen` makes one), which build route is
used, and whether tests exist. Fix what it flags.

## 2. Init: dry run, then write

```bash
ciqkit init                # prints a diff per file, changes nothing
ciqkit init --write --ci   # apply; --ci adds the GitHub workflow
```

It adds `ciqkit.toml`, merges `.mcp.json`, adds a marked block to `CLAUDE.md`
and missing `.gitignore` entries. Existing files are never overwritten, and a
second `--write` changes nothing. Details: [init.md](init.md). Read the diff,
particularly the `[build]` guess (see
[Coming from a custom build](#coming-from-a-custom-build)).

## 3. First shot

```bash
ciqkit sim shot home --native --out-dir shots
```

Look at the PNG. If it is the wrong screen or a blank, run
`ciqkit doctor --project .` again and see [sim.md](sim.md#known-limits). Use
`--native` for anything you might commit: without it the image contains
Garmin's device artwork.

## 4. One baseline per screen

Name each screen in `ciqkit.toml` with the steps to reach it and text that must
be on it. Steps are the same gesture objects as `sim nav --steps`
(`tap` with `x`/`y` fractions, `swipe` with `direction`, `key` with `key`):

```toml
[sim]
device = "venu3"

[[screens]]
name   = "home"
anchor = "12:00"

[[screens]]
name   = "settings"
steps  = [{kind = "tap", x = 0.5, y = 0.8}]
anchor = ["Settings", "Units"]
```

```bash
ciqkit sim shots --all --native --out-dir baselines
```

Full schema, including `seed` and `storage` per screen:
[config.md](config.md). Anchors are a confirmation, not a gate: without an OCR
backend (`pip install "ciqkit[sim,ocr]"`) they are skipped rather than failed.
Known limit: on rectangular devices the shots are currently square crops that
clip about 40 px at the top and bottom.

## 5. Checks in CI

`ciqkit init --ci` writes `.github/workflows/ciqkit-checks.yml`. It needs no
simulator: it runs `ciqkit layout --fail-under 80` and
`ciqkit shot-score --fail-under 70` on the PNGs committed in `baselines/`
(and does nothing if there is no such directory). So the convention is:
regenerate on your Mac, commit, let CI check.

```bash
ciqkit sim shots --all --native --out-dir baselines
git add baselines && git commit -m "Update baselines"
```

Commit only `--native` shots, never framed ones. Both scores are advisory
(see [checks.md](checks.md)); a score that drops against a screen's own
baseline is the real signal.

## 6. The agent loop

Point an agent at the project (the `.mcp.json` from step 2) and let it run:
change, `ciqsim_test`, `ciqsim_shots`, look, fix. How to set it up and what to
ask: [tutorial/04-with-an-agent.md](tutorial/04-with-an-agent.md).
`ciqkit init` writes the `garmin-sdk` server as `npx -y ciqkit-sdk-mcp`, which
works only once that package is published (it is not yet); until then point it
at `tools/sdk-mcp/scripts/run-server.sh` in a ciqkit clone
([sdk-mcp.md](sdk-mcp.md)).

## 7. Unit tests

Write `(:test)` functions (compiled only with `monkeyc -t`) and run:

```bash
ciqkit sim test
```

With the built-in build ciqkit compiles the `-t` variant itself. With a custom
build it cannot know how, so give it `[build] test_cmd` or pass `--prg` with a
test build (see below).

## 8. Seeds and storage

* `--seed NAME=VALUE` (or `seed = [...]` on a screen) patches a one-line
  `const NAME = VALUE;` in `source/Constants.mc` for one job and restores the
  file byte for byte. It refuses to patch a file with uncommitted changes.
  See [sim.md](sim.md).
* Runs start from **empty app storage** by default. `--storage keep` inherits
  the simulator's, `--storage DIR` loads a fixture, and `ciqkit storage`
  journals and replays states. A screen can pin one with `storage = "..."`.

## Coming from a custom build

If you have `scripts/build.sh`, a Makefile or your own `monkeyc` line, tell
ciqkit about it so it does not use its built-in build:

```toml
[build]
cmd      = "make dev DEVICE={device}"    # normal build
test_cmd = "make test DEVICE={device}"   # produces the `monkeyc -t` variant
```

* `{device}` is replaced with the device id.
* ciqkit **runs that command in your project directory**; see
  [SECURITY.md](../SECURITY.md#2-pointing-ciqkit-at-a-project-runs-that-projects-code).
* A `scripts/build.sh` is picked up automatically as `./scripts/build.sh -d
  {device}`; `ciqkit init` writes other guesses (`build.sh`, Makefile) as
  commented-out lines for you to check.
* Precedence for the build command: environment `CIQKIT_BUILD_CMD` >
  `[build] cmd` > `scripts/build.sh` > built-in `monkeyc`. The `--prg PATH`
  option skips building entirely and uses an existing `.prg`. Full table:
  [config.md](config.md#precedence).
* Device: `--device` > `[sim] device` > first manifest device installed in the
  SDK > `venu3`.

Without `test_cmd`, a custom `cmd` has no test variant, so `ciqkit sim test`
needs `--prg PATH` to a test PRG you built (or set `test_cmd`).
