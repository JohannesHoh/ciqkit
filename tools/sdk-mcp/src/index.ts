#!/usr/bin/env node
/**
 * Garmin Connect IQ SDK MCP Server
 *
 * Provides tools for looking up SDK API docs, searching the API,
 * querying a curated gotchas database, and device info.
 *
 * Transport: stdio (for VS Code integration)
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { indexPath } from "./paths.js";
import { loadData, registerTools } from "./tools.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// CIQKIT_SDK_MCP_DATA_DIR (absolute path) lets a project vendor the bundled
// server with its own data: gotchas.json is read from there, and an
// sdk-index.json in it wins over the user cache. An explicit dir beats the
// cache; without it the index lives in the user cache and
// data/sdk-index.json is the legacy fallback, still read so existing clones
// keep working.
const override = process.env.CIQKIT_SDK_MCP_DATA_DIR;
// A relative override is ignored; an absolute one is still used when it lacks
// gotchas.json (its sdk-index.json may be there), but then serves 0 gotchas.
// Either way say so, or the server runs quietly without its gotchas.
if (override && !path.isAbsolute(override)) {
  console.error(
    `ciqkit-sdk-mcp: CIQKIT_SDK_MCP_DATA_DIR=${override} ignored (needs an absolute dir containing gotchas.json)`,
  );
} else if (override && !fs.existsSync(path.join(override, "gotchas.json"))) {
  console.error(
    `ciqkit-sdk-mcp: CIQKIT_SDK_MCP_DATA_DIR=${override} has no gotchas.json; using it anyway (its sdk-index.json is still read) but serving 0 gotchas`,
  );
}
const dataDir =
  override && path.isAbsolute(override) ? override : path.join(__dirname, "..", "data");
const localIndex = path.join(dataDir, "sdk-index.json");
loadData(
  dataDir,
  dataDir === override ? [localIndex, indexPath()] : [indexPath(), localIndex],
);

const server = new McpServer({
  name: "ciqkit-sdk",
  version: "0.1.0",
});

registerTools(server);

async function main(): Promise<void> {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((err) => {
  console.error("Fatal error:", err);
  process.exit(1);
});
