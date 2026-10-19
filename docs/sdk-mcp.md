# `tools/sdk-mcp` — Connect IQ SDK lookup over MCP

An MCP server that answers Connect IQ API questions from the SDK documentation
**already installed on your machine**, plus a curated database of Monkey C
pitfalls.

It exists because Monkey C signatures are easy to get subtly wrong from memory
and the mistakes are expensive: `dc.setColor` is always two arguments,
`dc.clear()` takes none, `Graphics.FONT_XLARGE` does not exist, and
`Lang.format` uses `$1$` placeholders rather than `printf` ones. Each of those
costs a build cycle to discover. Looking the answer up first is cheaper.

## It ships no Garmin documentation

The API index is **generated locally** from the SDK docs on your disk, written
to your user cache, and never part of the repository or the npm package. That
documentation is Garmin's copyrighted material; this project redistributes none
of it. What is published is the *parser* and the gotchas database, both
original work.

This means the server is useful the moment you install it — the gotchas work
immediately — but API lookups need one setup command.

## Install and set up

There are two routes. **Status: `ciqkit-sdk-mcp` has not been published to npm
yet** (the release workflow is written but unrun), so until it is, use the
clone route; the npx commands below describe how it will work once it is out.

### Route A: npx (after the first npm release)

```bash
npx -p ciqkit-sdk-mcp ciqkit-sdk-parse     # once, and after each SDK upgrade
```

```jsonc
// .mcp.json  (what `ciqkit new` / `ciqkit init` write)
{
  "mcpServers": {
    "garmin-sdk": { "command": "npx", "args": ["-y", "ciqkit-sdk-mcp"] }
  }
}
```

### Route B: from a clone (works today)

```bash
cd tools/sdk-mcp
npm install
npm run parse-sdk     # same parser as ciqkit-sdk-parse
npm run build
```

```jsonc
// .mcp.json
{
  "mcpServers": {
    "garmin-sdk": {
      "command": "bash",
      "args": ["/path/to/ciqkit/tools/sdk-mcp/scripts/run-server.sh"]
    }
  }
}
```

`run-server.sh` builds on first use, so a fresh clone works without a manual
build step. (`examples/tictactoe/.mcp.json` points at it relatively.)

### The parse command

The parser finds the newest installed SDK automatically, or takes a path:

```bash
npx -p ciqkit-sdk-mcp ciqkit-sdk-parse /path/to/connectiq-sdk-mac-9.1.0-.../doc
CIQ_HOME=/path/to/sdk npx -p ciqkit-sdk-mcp ciqkit-sdk-parse
```

(`npm run parse-sdk -- <path>` in a clone.) Re-run it after installing a new SDK
version, then restart the MCP server.

### Where the index lives

`~/.cache/ciqkit/sdk-index.json`, or `$XDG_CACHE_HOME/ciqkit/sdk-index.json`
when that is set to an absolute path. It is a per-user cache, so it survives
`npx` re-downloads and package upgrades. Earlier clones kept it at
`tools/sdk-mcp/data/sdk-index.json` (gitignored); that file is still read as a
fallback if the cache copy is missing, but the parser always writes to the
cache. Until an index exists, API tools return an explanation of how to
generate it rather than a misleading "not found".

### Vendoring a single-file build

`npm run bundle` (in `tools/sdk-mcp`, after `npm install`) writes
`dist/ciqkit-sdk-mcp.mjs`: the whole server in one ESM file that needs only
Node 18+ at runtime — no `node_modules`. Copy it into your project and point it
at a data directory of your own:

```jsonc
{
  "mcpServers": {
    "garmin-sdk": {
      "command": "node",
      "args": ["/path/to/project/tools/ciqkit-sdk-mcp.mjs"],
      "env": { "CIQKIT_SDK_MCP_DATA_DIR": "/path/to/project/tools/sdk-mcp-data" }
    }
  }
}
```

`CIQKIT_SDK_MCP_DATA_DIR` must be an absolute path (a relative one is ignored).
When set, `gotchas.json` is read from that directory, and an `sdk-index.json`
there, if present, takes precedence over the user cache — the cache is the
fallback. Unset, the server reads `data/` next to its own `dist/` as before.

## Tools

| Tool | Answers |
| --- | --- |
| `garmin_lookup_api` | "What is on `Toybox.Graphics.Dc`?" — methods, fields, enums, inheritance, `since` API level |
| `garmin_search_sdk` | free-text search across modules, classes and methods |
| `garmin_list_module` | everything in one module |
| `garmin_get_gotchas` | the curated pitfalls database, filterable by API, severity, tag or text |
| `garmin_get_device_info` | buttons, touch support, back gesture and display type per device |

## The gotchas database

`data/gotchas.json` is 52 entries written from things that actually broke,
each with the wrong approach, the correct one, and code for both. Examples:

* container-access warnings in Monkey C are expected, not errors;
* `System.getTimer()` is a **signed** 32-bit value and goes negative after ~24.8
  days of uptime, so comparing raw timer values against a deadline eventually
  locks your app;
* an `ActivityRecording` session is a system-wide slot that must be released
  before the app exits, or the next launch cannot create one;
* "Watchdog Tripped" is a callback that ran too long, not an out-of-memory
  condition — and caching a result keyed on an input that grows every tick does
  not fix it, because the cache invalidates every tick.

Entries are editable: the server re-reads `gotchas.json` when its mtime changes,
so you can add your own without restarting anything. Contributions of
well-evidenced entries are welcome — see [CONTRIBUTING.md](../CONTRIBUTING.md).

### `device-info` accuracy

`garmin_get_device_info` is a hand-maintained table of button layouts, touch
support and display types. It covers the common Venu / Forerunner / fenix /
Instinct / D2 / MARQ / Approach families and is accurate as of the 9.1.0 SDK,
but it is **not** generated from the SDK and will drift as devices ship. For
anything load-bearing, check `compiler.json` in your SDK's device directory.

## Limits

* Only as current as the SDK you parsed. Re-run the parse command after an upgrade.
* The parser reads Garmin's generated HTML. A future documentation redesign can
  break it; if module counts drop suddenly, that is the cause.
* Descriptions are the SDK's own prose, extracted verbatim into your local
  index. Treat the index as a cache of Garmin's docs — do not publish it.
