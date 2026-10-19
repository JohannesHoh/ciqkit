# `ciqkit new` and `ciqkit init`

Short reference. Step by step: [tutorial](tutorial/README.md) for a new project,
[adopt.md](adopt.md) for an existing one.

## `ciqkit new NAME`

```bash
ciqkit new my-face --type watchface --devices venu3,fr255
cd my-face && ciqkit sim shot home --native
```

| Flag | Meaning |
|---|---|
| `--dir PATH` | Parent directory to create `NAME` in (default: current directory). |
| `--template blank\|tictactoe` | `blank` (default) is a minimal project; `tictactoe` is a copy of `examples/tictactoe` (watch apps only). |
| `--type app\|watchface\|datafield` | Manifest type (`watch-app`, `watchface`, `datafield`) and matching base classes. |
| `--devices d1,d2` | Target devices. Default: `venu3` if installed in the SDK, else the first installed device; for `--template tictactoe`, the example's three (`venu3,fr255,venusq2`). Devices that are not installed, or an SDK that cannot be found, produce a warning, not an error; an id with characters other than letters, digits, `_` and `-` is an error. |

`NAME` may use letters, digits, `.`, `_` and `-`; it becomes the directory, the
build output name and (CamelCased) the class prefix. The directory must not
exist or must be empty. Each project gets a fresh application id (UUID v4).
If you have no developer key, one is created (`ciqkit keygen`); if `openssl` is
missing that step is skipped with a warning. stdout is one JSON line, the next
steps go to stderr.

The generated project uses ciqkit's built-in build: there is deliberately no
`scripts/build.sh` (a build script makes ciqkit treat the build as custom, and
`ciqkit sim test` then needs `[build] test_cmd`).

Templates ship inside the package (`src/ciqkit/templates/`) under MIT-0, like
`examples/`. The generated `.mcp.json` starts the SDK server with
`npx -y ciqkit-sdk-mcp`.

## `ciqkit init [DIR]`

```bash
ciqkit init                # dry run: prints a unified diff per file
ciqkit init --write --ci   # apply, and add the CI workflow
```

For a project that already has `manifest.xml`. Planned changes:

* `ciqkit.toml` -- created only if absent: `[sim] device` from the manifest, a
  `home` screen, and `[build] cmd` when a build script is detected
  (`scripts/build.sh` is used as is; `build.sh`, `Makefile` and friends are
  written as a commented-out guess for you to check).
* `.mcp.json` -- created or merged: `ciqsim` and `garmin-sdk` are added unless
  present (an SDK server is recognised by name or by a command that runs
  `ciqkit-sdk-mcp`); other entries are untouched. Invalid JSON is left alone.
* `CLAUDE.md` -- a block between `<!-- ciqkit:begin -->` and
  `<!-- ciqkit:end -->` is appended, or replaced if already there. Nothing
  outside the markers changes, line endings included. A begin marker with no
  end marker after it is left alone (see below).
* `.gitignore` -- only missing entries (`bin/`, `*.prg`, `*.iq`, `*.der`,
  `*.pem`, `*.jks`, `shots/`) are appended.
* `--ci` -- `.github/workflows/ciqkit-checks.yml`, running `ciqkit layout` and
  `ciqkit shot-score` on committed screenshots in `baselines/`.

An existing `ciqkit.toml` or workflow is never touched; it is reported as
`skip`. Running `init --write` twice changes nothing the second time. `--json`
prints the plan as one JSON object instead of diffs.

Some files cannot be merged safely and are reported as `ATTENTION` instead:
a `.mcp.json` that is not valid JSON (or not an object), a `CLAUDE.md` with an
unmatched begin marker, and any target that is a symlink or sits under one
(`init` never writes through a link, so a checkout cannot point it at a file
outside the project). Nothing is written to those files, and `init` exits 1 so
a script does not mistake the run for a finished setup; fix them by hand and
run it again.
