#!/usr/bin/env bash
# Launcher for the ciqkit SDK MCP server.
#
# Resolves its own location, so it works from whatever cwd the MCP client
# happens to use. Builds once on first run; after that it is a plain exec.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

if [ ! -f dist/index.js ]; then
  echo "ciqkit-sdk-mcp: first run, building..." >&2
  # Lockfile only: no `npm install` fallback, which would resolve unpinned
  # versions. The npm package ships dist/, so this path is checkout-only and
  # package-lock.json is always there.
  if [ ! -d node_modules ]; then
    npm ci >&2
  fi
  npm run build >&2
fi

# Index location: $XDG_CACHE_HOME/ciqkit/ or ~/.cache/ciqkit/ (data/ is the
# legacy location from before the npm package, still read as a fallback).
INDEX="${XDG_CACHE_HOME:-$HOME/.cache}/ciqkit/sdk-index.json"
if [ ! -f "$INDEX" ] && [ ! -f data/sdk-index.json ]; then
  echo "ciqkit-sdk-mcp: no SDK index yet ($INDEX). API lookups will explain" >&2
  echo "  how to generate it; the gotchas database works without it." >&2
  echo "  To generate:  cd $DIR && npm run parse-sdk" >&2
fi

exec node dist/index.js
