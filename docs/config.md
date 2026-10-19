# `ciqkit.toml`

Optional, per project: a `ciqkit.toml` in the project directory (next to
`manifest.xml`). It holds the settings you would otherwise repeat on every
command line, and named screens you can regenerate in one go. No file means
every default applies. Unknown keys are logged as a warning and ignored; a
malformed file is an error that names the file and line.

```toml
[build]
cmd      = "make dev DEVICE={device}"     # normal build; {device} is substituted
test_cmd = "make test DEVICE={device}"    # build that produces the `monkeyc -t` variant

[sim]
device = "venu3"

[[screens]]
name   = "home"
anchor = "12:00"                          # string or list of strings (OCR, like --anchor)

[[screens]]
name    = "settings"
steps   = [                               # same gesture objects as `sim nav --steps`
  {kind = "tap", x = 0.5, y = 0.8},
]
anchor  = ["Settings", "Units"]           # checked on the screen the steps end on
seed    = ["SEED_DEMO_DATA=true"]         # same NAME=VALUE as --seed
storage = "fixtures/full"                 # fresh | keep | a fixture dir (relative to the project)
```

A `[[screens]]` entry is just saved CLI arguments: `steps`, `anchor`, `seed` and
`storage` mean exactly what `--steps`, `--anchor`, `--seed` and `--storage` mean
for `sim nav` / `sim shot`. `name` becomes the file name (`<name>.png`) and must
be unique and a plain file name.

## Regenerating screens

```bash
ciqkit sim shots --all                    # every [[screens]] entry
ciqkit sim shots --screen home --screen settings
ciqkit sim shots --all --out-dir shots/   # default: the same private ciqkit dir as `sim shot`
```

Each screen is its own broker job (so other work can still interleave between
screens), submitted in file order and **cold-loaded** so a screen never inherits
the previous one's navigation, seed or storage. One JSON line per screen is
printed — `{screen, ok, path, job_id, error?}` — and the exit code is non-zero
if any screen failed (the rest still run). `--device`, `--prg`, `--storage`,
`--native`, `--no-crop` and `--priority` apply to every screen; `--storage`
overrides a screen's own. `--prg` cannot be combined with a screen that has a
`seed` (the seed would be silently ignored). MCP: `ciqsim_shots`.

## Precedence

Highest first. A rung that does not exist is simply skipped.

| Setting | CLI / MCP argument | Environment | `ciqkit.toml` | Default |
|---|---|---|---|---|
| Build command | (`build_cmd` argument, library only) | `CIQKIT_BUILD_CMD` | `[build] cmd` | `scripts/build.sh`, then built-in `monkeyc` |
| Unit-test build | `--prg` | — | `[build] test_cmd` | built-in `monkeyc -t` (a custom `cmd` has no test variant, so `--prg` is required) |
| Device | `--device` | — (none exists) | `[sim] device` | first manifest product installed in the SDK, else `venu3` |
| Storage / anchor / seed / steps | `--storage` (shots only) | — | per `[[screens]]` | fresh storage, none |

`ciqkit doctor --project DIR` validates the file and reports which build route
is in effect.

## Python 3.10

The base install has no dependencies. On Python 3.11+ the file is read with
`tomllib`; on 3.10 `tomli` is used if it is installed, otherwise ciqkit's small
built-in reader, which supports only what this schema needs: tables, arrays of
tables, strings, integers, floats, booleans, arrays and inline tables. Anything
outside that subset is reported as an error with its line number rather than
misread.
