# CLAUDE.md

Guidance for AI agents (and people) working on this Connect IQ project.

## What this is

`{{DISPLAY_NAME}}`: a Connect IQ `{{MANIFEST_TYPE}}`, started with `ciqkit new`.
Targets: {{DEVICES}}. Add a device by adding a `<iq:product id="..."/>` line to
`manifest.xml` (it must be installed in the SDK Manager).

## Commands

Run from this directory. ciqkit builds the app itself inside each `sim` job
(no build script), so there is no separate build step.

```bash
ciqkit doctor --project .             # SDK, devices, key, build, tests
ciqkit sim test                       # unit tests in source/tests/
ciqkit sim shot NAME --native --anchor TEXT   # screenshot + OCR check
ciqkit sim shots --all --native --out-dir shots   # every [[screens]] in ciqkit.toml
ciqkit layout --round shots           # --rect for rectangular devices
```

## Code map

* `source/{{ID}}App.mc` -- application entry (`getInitialView`).
* `source/{{ID}}View.mc` -- drawing.
* `source/tests/` -- `(:test)` functions, compiled only by `monkeyc -t`, which
  `ciqkit sim test` does for you. Do not add `base.excludeAnnotations = test`
  to `monkey.jungle`: it strips the tests even under `-t`.
* `ciqkit.toml` -- device and named screens. Add a `[[screens]]` entry for
  every screen you add.

{{CIQKIT_BLOCK}}
