# 5. Your own app

## Create it

```bash
ciqkit new my-app --devices venu3,fr255
cd my-app
ciqkit doctor --project .
ciqkit sim shot home --native --out-dir shots
```

Once you know what text your first screen shows, add `--anchor TEXT` (or an
`anchor` in `ciqkit.toml`) so a wrong screen becomes an error.

`ciqkit new` options, all verified against `ciqkit new --help`:

| Flag | Meaning |
|---|---|
| `--type app\|watchface\|datafield` | what kind of Connect IQ project (default `app`, i.e. a watch app) |
| `--devices d1,d2` | target devices (default: `venu3` if installed) |
| `--template blank\|tictactoe` | `blank` (default) or a copy of the example |
| `--dir PATH` | parent directory to create it in |

```bash
ciqkit new my-face --type watchface --devices venu3
ciqkit new my-field --type datafield
```

You get a manifest, `monkey.jungle`, a small source tree, a unit-test skeleton,
`ciqkit.toml` with a `home` screen, `.mcp.json`, a `CLAUDE.md` for agents, a
`.gitignore`, and (if you have none) a developer key. All options and what is
generated: [../init.md](../init.md).

## The app id

Every project gets a fresh application id (UUID) in `manifest.xml`, so you do
not have to do anything for a new project. If you instead **copy** an existing
project by hand (including the example), change the `id` yourself:

```bash
python3 -c "import uuid; print(uuid.uuid4())"
```

Two apps with the same id replace each other on a watch and clash in the
Connect IQ store.

## Build without ciqkit

ciqkit builds inside its own jobs. To compile by hand, use `monkeyc` from the
SDK's `bin/` directory:

```bash
monkeyc -f monkey.jungle -d venu3 -y ~/.config/ciqkit/developer_key.der -o bin/my-app.prg
```

## Where to go from here

* Add a `[[screens]]` entry per screen you care about, with `steps` and an
  `anchor`: [../config.md](../config.md).
* Write unit tests with `(:test)` functions and run `ciqkit sim test`.
* Commit native screenshots under `baselines/` and add the CI check:
  [../adopt.md](../adopt.md#5-checks-in-ci).
* Look at the SDK lookup server: [../sdk-mcp.md](../sdk-mcp.md).
* Publishing the app to the Connect IQ store is Garmin's process and not
  covered here; start at
  [developer.garmin.com/connect-iq](https://developer.garmin.com/connect-iq/).

## What you learned

* `ciqkit new` makes a project that is ready to build, test and screenshot.
* Always give a copied project its own app id.

That is the end of the tutorial. Reference docs are listed in the
[README](../../README.md#documentation).
