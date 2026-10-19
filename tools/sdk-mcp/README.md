# ciqkit-sdk-mcp

An MCP server that answers Connect IQ API questions from the SDK documentation
**already installed on your machine**, plus a curated database of Monkey C
pitfalls.

Part of [ciqkit](https://github.com/JohannesHoh/ciqkit). Unofficial; not
affiliated with or endorsed by Garmin Ltd. See
[TRADEMARKS.md](../../TRADEMARKS.md).

## It ships no Garmin documentation

The API index is generated locally from the SDK docs on your own disk, written
to `~/.cache/ciqkit/sdk-index.json`, and is neither committed nor included in
the npm package. That documentation is Garmin's copyrighted material and none of
it is redistributed here — what is published is the parser and the gotchas
database, both original work. See [NOTICE](../../NOTICE).

The gotchas work immediately; API lookups need one setup command.

## Quick start

**Not yet on npm** — the release workflow is written but has not been run. Until
the first release, use the clone route.

From npm (once published):

```bash
npx -p ciqkit-sdk-mcp ciqkit-sdk-parse    # reads your SDK docs, writes the index
```

```jsonc
{ "mcpServers": { "garmin-sdk": { "command": "npx", "args": ["-y", "ciqkit-sdk-mcp"] } } }
```

From a clone of the repository:

```bash
cd tools/sdk-mcp
npm install
npm run parse-sdk     # same parser as ciqkit-sdk-parse
npm run build
```

```jsonc
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
build step. The parser takes an SDK doc path as an argument or `CIQ_HOME`;
`$XDG_CACHE_HOME` moves the index. `tools/sdk-mcp/data/sdk-index.json` (the old
location) is still read as a fallback.

To vendor the server into another project, `npm run bundle` writes a
single-file `dist/ciqkit-sdk-mcp.mjs` that runs on plain Node with no
`node_modules`. Set `CIQKIT_SDK_MCP_DATA_DIR` to an absolute directory holding
your own `gotchas.json` (and optionally an `sdk-index.json`, which then wins
over the user cache). See [docs/sdk-mcp.md](../../docs/sdk-mcp.md#vendoring-a-single-file-build).

## Tools

| Tool | Answers |
| --- | --- |
| `garmin_lookup_api` | what is on a class — methods, fields, enums, inheritance, `since` API level |
| `garmin_search_sdk` | free-text search across modules, classes and methods |
| `garmin_list_module` | everything in one module |
| `garmin_get_gotchas` | the curated pitfalls database, filterable by API, severity, tag or text |
| `garmin_get_device_info` | buttons, touch support, back gesture and display type per device |

Requires Node 18+.

Full documentation: [docs/sdk-mcp.md](../../docs/sdk-mcp.md), including how the
index is generated, what `device-info` does and does not guarantee, and the
limits of parsing generated HTML.

## Licence

[Apache License 2.0](../../LICENSE), same as the rest of ciqkit.
