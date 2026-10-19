/**
 * MCP tool implementations for the Garmin SDK server.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { z } from "zod";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { SdkIndex, SdkModule, SdkClass, SdkMethod, SdkField, SdkEnum, Gotcha, DeviceInfo } from "./types.js";

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------

let sdkIndex: SdkIndex | null = null;

// Shown by every tool that needs the API index. The index is NOT shipped with
// this repo -- it is generated from the Connect IQ SDK documentation installed
// on your own machine, which stays on your machine. See docs/sdk-mcp.md.
const NO_INDEX_MESSAGE =
  "No SDK index found. This server does not ship Garmin's API documentation; " +
  "you generate it locally from your own SDK install:\n\n" +
  "    npx -p ciqkit-sdk-mcp ciqkit-sdk-parse\n\n" +
  "(in a clone of ciqkit: cd tools/sdk-mcp && npm install && npm run parse-sdk)\n\n" +
  "That writes ~/.cache/ciqkit/sdk-index.json (or $XDG_CACHE_HOME/ciqkit/). " +
  "Set CIQ_HOME first if your SDK is not in the default location, then " +
  "restart the MCP server. The gotchas database works without it.";
let gotchas: Gotcha[] = [];

// Path + mtime of gotchas.json, so the file can be hot-reloaded without an
// MCP-server restart. gotchas.json is edited far more often than the SDK
// index (every time a new pitfall is learned); requiring a restart would mean
// a freshly-added gotcha stays invisible until the next session, which is how
// a known pitfall gets hit anyway.
let gotchasPath = "";
let gotchasMtimeMs = 0;

// Device capabilities, hand-maintained. This table is NOT generated from the
// SDK, so it will drift as devices ship -- for anything load-bearing, check
// compiler.json in your SDK's device directory. `touch` is the field most
// callers care about: NOT every
// Garmin watch has a touchscreen, and notably no Instinct (even the AMOLED ones),
// the original Enduro, and the Forerunner 945/945 LTE are button-only. The 5-button
// layout (START/STOP, BACK/LAP, UP/MENU, DOWN, LIGHT) is the outdoor/multisport
// standard; AMOLED lifestyle watches (Venu/Vivoactive/Approach) are touch-first with
// 1–3 buttons. Resolutions follow Garmin's AMOLED size ladder (1.1"≈360, 1.2"≈390,
// 1.3"≈416, 1.4"≈454); a few aviation D2 sizes are omitted rather than guessed.
const BTN5 = ["START/STOP", "BACK/LAP", "UP/MENU", "DOWN", "LIGHT"];

const DEVICE_INFO: DeviceInfo[] = [
  // ── Venu (AMOLED, touch-first) ───────────────────────────────────────────
  { name: "venu2", category: "venu", buttons: ["UPPER (right)", "LOWER/BACK (right)"], touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "416x416", displayType: "AMOLED round" },
  { name: "venu2plus", category: "venu", buttons: ["UPPER (right)", "MIDDLE/ACTION (right)", "LOWER/BACK (right)"], touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "416x416", displayType: "AMOLED round" },
  { name: "venu2s", category: "venu", buttons: ["UPPER (right)", "LOWER/BACK (right)"], touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "360x360", displayType: "AMOLED round" },
  { name: "venu3", category: "venu", buttons: ["ACTION (right)", "BACK (left)"], touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "454x454", displayType: "AMOLED round" },
  { name: "venu3s", category: "venu", buttons: ["ACTION (right)", "BACK (left)"], touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "390x390", displayType: "AMOLED round" },
  { name: "venu441mm", category: "venu", aliases: ["venu4"], buttons: ["ACTION (right)", "BACK (left)"], touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "390x390", displayType: "AMOLED round" },
  { name: "venu445mm", category: "venu", buttons: ["ACTION (right)", "BACK (left)"], touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "454x454", displayType: "AMOLED round" },
  { name: "venusq2", category: "venu", buttons: ["UPPER (right)", "LOWER (right)"], touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "320x360", displayType: "AMOLED rectangle" },
  { name: "venusq2m", category: "venu", aliases: ["venusq2music"], buttons: ["UPPER (right)", "LOWER (right)"], touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "320x360", displayType: "AMOLED rectangle" },

  // ── Vivoactive (AMOLED, touch-first) ─────────────────────────────────────
  { name: "vivoactive5", category: "vivoactive", buttons: ["ACTION (right)"], touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "390x390", displayType: "AMOLED round" },
  { name: "vivoactive6", category: "vivoactive", buttons: ["ACTION (right)"], touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "390x390", displayType: "AMOLED round" },

  // ── Forerunner ───────────────────────────────────────────────────────────
  { name: "fr165", category: "forerunner", aliases: ["forerunner165"], buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "390x390", displayType: "AMOLED round" },
  { name: "fr165m", category: "forerunner", aliases: ["fr165music"], buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "390x390", displayType: "AMOLED round" },
  { name: "fr265", category: "forerunner", aliases: ["forerunner265"], buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "416x416", displayType: "AMOLED round" },
  { name: "fr265s", category: "forerunner", aliases: ["forerunner265s"], buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "360x360", displayType: "AMOLED round" },
  { name: "fr57042mm", category: "forerunner", aliases: ["fr570", "forerunner570"], buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "390x390", displayType: "AMOLED round" },
  { name: "fr57047mm", category: "forerunner", buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "454x454", displayType: "AMOLED round" },
  { name: "fr945", category: "forerunner", aliases: ["forerunner945"], buttons: BTN5, touch: false, backMethod: "BACK button", displaySize: "240x240", displayType: "MIP round (no touch)" },
  { name: "fr945lte", category: "forerunner", aliases: ["forerunner945lte"], buttons: BTN5, touch: false, backMethod: "BACK button", displaySize: "240x240", displayType: "MIP round (no touch)" },
  { name: "fr955", category: "forerunner", aliases: ["forerunner955"], buttons: BTN5, touch: true, backMethod: "BACK button", displaySize: "260x260", displayType: "MIP round" },
  { name: "fr965", category: "forerunner", aliases: ["forerunner965"], buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "454x454", displayType: "AMOLED round" },
  { name: "fr970", category: "forerunner", aliases: ["forerunner970"], buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "454x454", displayType: "AMOLED round" },

  // ── fenix (MIP unless noted; touch on gen7+, never on the original Enduro) ─
  { name: "fenix7", category: "fenix", buttons: BTN5, touch: true, backMethod: "BACK button", displaySize: "260x260", displayType: "MIP round" },
  { name: "fenix7s", category: "fenix", buttons: BTN5, touch: true, backMethod: "BACK button", displaySize: "240x240", displayType: "MIP round" },
  { name: "fenix7x", category: "fenix", buttons: BTN5, touch: true, backMethod: "BACK button", displaySize: "280x280", displayType: "MIP round" },
  { name: "fenix7pro", category: "fenix", buttons: BTN5, touch: true, backMethod: "BACK button", displaySize: "260x260", displayType: "MIP round" },
  { name: "fenix7spro", category: "fenix", buttons: BTN5, touch: true, backMethod: "BACK button", displaySize: "240x240", displayType: "MIP round" },
  { name: "fenix7xpro", category: "fenix", buttons: BTN5, touch: true, backMethod: "BACK button", displaySize: "280x280", displayType: "MIP round" },
  { name: "fenix7pronowifi", category: "fenix", buttons: BTN5, touch: true, backMethod: "BACK button", displaySize: "260x260", displayType: "MIP round" },
  { name: "fenix7xpronowifi", category: "fenix", buttons: BTN5, touch: true, backMethod: "BACK button", displaySize: "280x280", displayType: "MIP round" },
  { name: "fenix843mm", category: "fenix", buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "416x416", displayType: "AMOLED round" },
  { name: "fenix847mm", category: "fenix", aliases: ["fenix8"], buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "454x454", displayType: "AMOLED round" },
  { name: "fenix8pro47mm", category: "fenix", aliases: ["fenix8pro"], buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "454x454", displayType: "AMOLED round" },
  { name: "fenix8solar47mm", category: "fenix", aliases: ["fenix8solar"], buttons: BTN5, touch: true, backMethod: "BACK button", displaySize: "260x260", displayType: "MIP round" },
  { name: "fenix8solar51mm", category: "fenix", buttons: BTN5, touch: true, backMethod: "BACK button", displaySize: "280x280", displayType: "MIP round" },
  { name: "fenixe", category: "fenix", buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "416x416", displayType: "AMOLED round" },

  // ── epix (AMOLED, touch) ─────────────────────────────────────────────────
  { name: "epix2", category: "epix", aliases: ["epixgen2", "epix2gen2"], buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "416x416", displayType: "AMOLED round" },
  { name: "epix2pro42mm", category: "epix", buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "390x390", displayType: "AMOLED round" },
  { name: "epix2pro47mm", category: "epix", aliases: ["epix2pro"], buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "416x416", displayType: "AMOLED round" },
  { name: "epix2pro51mm", category: "epix", buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "454x454", displayType: "AMOLED round" },

  // ── Instinct (button-only — NO touchscreen, even on AMOLED models) ────────
  { name: "instinct3amoled45mm", category: "instinct", aliases: ["instinct3amoled"], buttons: BTN5, touch: false, backMethod: "BACK button", displaySize: "390x390", displayType: "AMOLED round (no touch)" },
  { name: "instinct3amoled50mm", category: "instinct", buttons: BTN5, touch: false, backMethod: "BACK button", displaySize: "416x416", displayType: "AMOLED round (no touch)" },
  { name: "instinctcrossoveramoled", category: "instinct", aliases: ["instinctcrossover"], buttons: BTN5, touch: false, backMethod: "BACK button", displaySize: "390x390", displayType: "AMOLED round, analog-hand cutout (no touch)" },

  // ── Enduro (original = no touch; Enduro 3 added a MIP touchscreen) ────────
  { name: "enduro", category: "enduro", buttons: BTN5, touch: false, backMethod: "BACK button", displaySize: "280x280", displayType: "MIP round (no touch)" },
  { name: "enduro3", category: "enduro", buttons: BTN5, touch: true, backMethod: "BACK button", displaySize: "280x280", displayType: "MIP round" },

  // ── Descent (dive — AMOLED touch, disabled underwater/in dive activities) ─
  { name: "descentg2", category: "descent", buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "390x390", displayType: "AMOLED round" },
  { name: "descentmk343mm", category: "descent", aliases: ["descentmk3"], buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "390x390", displayType: "AMOLED round" },
  { name: "descentmk351mm", category: "descent", buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "454x454", displayType: "AMOLED round" },

  // ── D2 (aviation — AMOLED touch; exact panel sizes vary, left unspecified) ─
  { name: "d2airx10", category: "d2", buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displayType: "AMOLED round" },
  { name: "d2mach1", category: "d2", buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displayType: "AMOLED round" },
  { name: "d2mach2", category: "d2", buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displayType: "AMOLED round" },

  // ── MARQ (AMOLED, touch) ─────────────────────────────────────────────────
  { name: "marq2", category: "marq", buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "454x454", displayType: "AMOLED round" },
  { name: "marq2aviator", category: "marq", buttons: BTN5, touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "454x454", displayType: "AMOLED round" },

  // ── Approach (golf — AMOLED, touch-first) ────────────────────────────────
  { name: "approachs50", category: "approach", buttons: ["MENU/POWER (right)"], touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "390x390", displayType: "AMOLED round" },
  { name: "approachs7042mm", category: "approach", aliases: ["approachs70"], buttons: ["MENU/POWER (right)"], touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "390x390", displayType: "AMOLED round" },
  { name: "approachs7047mm", category: "approach", buttons: ["MENU/POWER (right)"], touch: true, backMethod: "SWIPE_RIGHT or BACK button", displaySize: "454x454", displayType: "AMOLED round" },
];

export function loadData(dataDir: string, indexCandidates: string[]): void {
  for (const sdkPath of indexCandidates) {
    if (!fs.existsSync(sdkPath)) continue;
    try {
      sdkIndex = JSON.parse(fs.readFileSync(sdkPath, "utf-8")) as SdkIndex;
      break;
    } catch (err) {
      console.error(`ciqkit-sdk-mcp: ignoring unreadable index ${sdkPath}: ${err}`);
    }
  }

  gotchasPath = path.join(dataDir, "gotchas.json");
  reloadGotchasIfChanged();
}

// Re-read gotchas.json when its mtime has changed since the last load. Called
// at startup and before every garmin_get_gotchas query, so edits to the file
// take effect immediately — no MCP-server restart needed.
function reloadGotchasIfChanged(): void {
  if (!gotchasPath || !fs.existsSync(gotchasPath)) return;
  try {
    const mtime = fs.statSync(gotchasPath).mtimeMs;
    if (mtime === gotchasMtimeMs) return;
    gotchas = JSON.parse(fs.readFileSync(gotchasPath, "utf-8")) as Gotcha[];
    gotchasMtimeMs = mtime;
  } catch {
    // Keep the last good copy if the file is mid-write or malformed.
  }
}

// ---------------------------------------------------------------------------
// Search helpers
// ---------------------------------------------------------------------------

function findModule(name: string): SdkModule | undefined {
  if (!sdkIndex) return undefined;
  const lower = name.toLowerCase();
  return sdkIndex.modules.find(m =>
    m.name.toLowerCase() === lower || m.fullName.toLowerCase() === `toybox.${lower}`
  );
}

function findClass(moduleName: string, className: string): SdkClass | undefined {
  const mod = findModule(moduleName);
  if (!mod) return undefined;
  const lower = className.toLowerCase();
  return mod.classes.find(c => c.name.toLowerCase() === lower);
}

function formatMethod(m: SdkMethod): string {
  const params = m.params.map(p => `${p.name}: ${p.type}`).join(", ");
  let out = `  ${m.name}(${params}) → ${m.returnType}`;
  if (m.since) out += ` [since ${m.since}]`;
  if (m.description) out += `\n    ${m.description}`;
  if (m.throws) out += `\n    Throws: ${m.throws}`;
  return out;
}

function formatField(f: SdkField): string {
  let out = `  ${f.name}: ${f.type}`;
  if (f.since) out += ` [since ${f.since}]`;
  if (f.description) out += `\n    ${f.description}`;
  return out;
}

function formatEnum(e: SdkEnum): string {
  const header = `  ${e.name}${e.since ? ` [since ${e.since}]` : ""}`;
  const values = e.values.map(v =>
    `    ${v.name} = ${v.value}${v.since ? ` [since ${v.since}]` : ""}${v.description ? ` — ${v.description}` : ""}`
  ).join("\n");
  return `${header}\n${values}`;
}

function formatClass(c: SdkClass): string {
  const parts: string[] = [];
  parts.push(`Class: ${c.fullName}`);
  if (c.inherits) parts.push(`Inherits: ${c.inherits}`);
  if (c.since) parts.push(`Since: API Level ${c.since}`);
  if (c.description) parts.push(`\n${c.description}`);

  if (c.fields.length > 0) {
    parts.push(`\nFields (${c.fields.length}):`);
    c.fields.forEach(f => parts.push(formatField(f)));
  }

  if (c.methods.length > 0) {
    parts.push(`\nMethods (${c.methods.length}):`);
    c.methods.forEach(m => parts.push(formatMethod(m)));
  }

  if (c.enums.length > 0) {
    parts.push(`\nEnums (${c.enums.length}):`);
    c.enums.forEach(e => parts.push(formatEnum(e)));
  }

  if (c.childClasses && c.childClasses.length > 0) {
    parts.push(`\nChild classes: ${c.childClasses.join(", ")}`);
  }

  return parts.join("\n");
}

function formatModule(m: SdkModule, detailed: boolean): string {
  const parts: string[] = [];
  parts.push(`Module: ${m.fullName}`);
  if (m.since) parts.push(`Since: API Level ${m.since}`);
  if (m.description) parts.push(`\n${m.description}`);

  if (m.classes.length > 0) {
    parts.push(`\nClasses (${m.classes.length}):`);
    m.classes.forEach(c => {
      if (detailed) {
        parts.push(`\n${"─".repeat(40)}`);
        parts.push(formatClass(c));
      } else {
        parts.push(`  ${c.name}${c.since ? ` [since ${c.since}]` : ""} — ${c.description.substring(0, 80)}`);
      }
    });
  }

  if (m.methods.length > 0) {
    parts.push(`\nModule methods (${m.methods.length}):`);
    m.methods.forEach(method => parts.push(formatMethod(method)));
  }

  if (m.enums.length > 0) {
    parts.push(`\nEnums (${m.enums.length}):`);
    m.enums.forEach(e => parts.push(formatEnum(e)));
  }

  return parts.join("\n");
}

function formatGotcha(g: Gotcha): string {
  const parts: string[] = [];
  parts.push(`[${g.id}] ${g.title} (${g.severity})`);
  parts.push(`API: ${g.api} | Tags: ${g.tags.join(", ")}`);
  parts.push(`WRONG: ${g.wrong}`);
  parts.push(`CORRECT: ${g.correct}`);
  if (g.codeWrong) parts.push(`\nWrong code:\n${g.codeWrong}`);
  if (g.codeCorrect) parts.push(`\nCorrect code:\n${g.codeCorrect}`);
  parts.push(`Source: ${g.source}`);
  return parts.join("\n");
}

// ---------------------------------------------------------------------------
// Register tools on the MCP server
// ---------------------------------------------------------------------------

export function registerTools(server: McpServer): void {

  // 1. garmin_lookup_api  — Look up a specific class or module
  server.tool(
    "garmin_lookup_api",
    "Look up a specific Garmin Connect IQ SDK class or module by name. Returns fields, methods, enums, and inheritance info.",
    {
      module: z.string().describe("Module name (e.g. 'Sensor', 'Activity', 'WatchUi', 'Graphics')"),
      className: z.string().optional().describe("Optional class name within the module (e.g. 'SensorData', 'Info', 'ConfirmationDelegate')"),
    },
    async ({ module: moduleName, className }) => {
      if (!sdkIndex) {
        return { content: [{ type: "text" as const, text: NO_INDEX_MESSAGE }] };
      }

      if (className) {
        const cls = findClass(moduleName, className);
        if (!cls) {
          // Try searching all modules
          const allClasses = sdkIndex.modules.flatMap(m => m.classes);
          const match = allClasses.find(c => c.name.toLowerCase() === className.toLowerCase());
          if (match) {
            return { content: [{ type: "text" as const, text: formatClass(match) }] };
          }
          return { content: [{ type: "text" as const, text: `Class '${className}' not found in module '${moduleName}'.` }] };
        }
        return { content: [{ type: "text" as const, text: formatClass(cls) }] };
      }

      const mod = findModule(moduleName);
      if (!mod) {
        return { content: [{ type: "text" as const, text: `Module '${moduleName}' not found. Available: ${sdkIndex.modules.map(m => m.name).join(", ")}` }] };
      }
      return { content: [{ type: "text" as const, text: formatModule(mod, false) }] };
    }
  );

  // 2. garmin_search_sdk — Full-text search across the SDK index
  server.tool(
    "garmin_search_sdk",
    "Search the Garmin Connect IQ SDK for methods, fields, enums, or classes matching a query string. Useful when you don't know which module something is in.",
    {
      query: z.string().describe("Search term (e.g. 'heartRate', 'addLap', 'SPORT_TRAINING', 'ConfirmationDelegate')"),
      maxResults: z.number().optional().default(20).describe("Maximum results to return (default: 20)"),
    },
    async ({ query, maxResults }) => {
      if (!sdkIndex) {
        return { content: [{ type: "text" as const, text: NO_INDEX_MESSAGE }] };
      }

      const q = query.toLowerCase();
      const results: string[] = [];

      for (const mod of sdkIndex.modules) {
        // Search module methods
        for (const m of mod.methods) {
          if (m.name.toLowerCase().includes(q) || m.description.toLowerCase().includes(q)) {
            results.push(`[Method] ${mod.name}.${m.name}() → ${m.returnType}\n  ${m.description.substring(0, 100)}`);
          }
        }

        // Search module enums
        for (const e of mod.enums) {
          for (const v of e.values) {
            if (v.name.toLowerCase().includes(q)) {
              results.push(`[Enum] ${mod.name}.${v.name} = ${v.value} (${e.name})`);
            }
          }
        }

        // Search classes
        for (const cls of mod.classes) {
          if (cls.name.toLowerCase().includes(q)) {
            results.push(`[Class] ${cls.fullName}${cls.inherits ? ` extends ${cls.inherits}` : ""}\n  ${cls.description.substring(0, 100)}`);
          }

          for (const m of cls.methods) {
            if (m.name.toLowerCase().includes(q) || m.description.toLowerCase().includes(q)) {
              results.push(`[Method] ${cls.fullName}.${m.name}() → ${m.returnType}\n  ${m.description.substring(0, 100)}`);
            }
          }

          for (const f of cls.fields) {
            if (f.name.toLowerCase().includes(q) || f.description.toLowerCase().includes(q)) {
              results.push(`[Field] ${cls.fullName}.${f.name}: ${f.type}\n  ${f.description.substring(0, 100)}`);
            }
          }

          for (const e of cls.enums) {
            for (const v of e.values) {
              if (v.name.toLowerCase().includes(q)) {
                results.push(`[Enum] ${cls.fullName}.${v.name} = ${v.value} (${e.name})`);
              }
            }
          }
        }

        if (results.length >= maxResults) break;
      }

      const truncated = results.slice(0, maxResults);
      const text = truncated.length > 0
        ? `Found ${results.length} results for "${query}"${results.length > maxResults ? ` (showing first ${maxResults})` : ""}:\n\n${truncated.join("\n\n")}`
        : `No results found for "${query}".`;

      return { content: [{ type: "text" as const, text }] };
    }
  );

  // 3. garmin_list_module — List all modules or classes in a module
  server.tool(
    "garmin_list_module",
    "List all Garmin Connect IQ SDK modules, or list all classes/methods within a specific module.",
    {
      module: z.string().optional().describe("Module name to list contents of. Omit to list all modules."),
    },
    async ({ module: moduleName }) => {
      if (!sdkIndex) {
        return { content: [{ type: "text" as const, text: NO_INDEX_MESSAGE }] };
      }

      if (!moduleName) {
        const list = sdkIndex.modules.map(m => {
          const classCount = m.classes.length;
          const methodCount = m.methods.length + m.classes.reduce((s, c) => s + c.methods.length, 0);
          return `  ${m.name} — ${classCount} classes, ${methodCount} methods${m.since ? ` [since ${m.since}]` : ""}`;
        }).join("\n");
        return { content: [{ type: "text" as const, text: `SDK ${sdkIndex.sdkVersion} — ${sdkIndex.modules.length} modules:\n\n${list}` }] };
      }

      const mod = findModule(moduleName);
      if (!mod) {
        return { content: [{ type: "text" as const, text: `Module '${moduleName}' not found.` }] };
      }
      return { content: [{ type: "text" as const, text: formatModule(mod, true) }] };
    }
  );

  // 4. garmin_get_gotchas — Query the gotchas database
  server.tool(
    "garmin_get_gotchas",
    "Search the curated database of Garmin Connect IQ SDK gotchas and pitfalls learned from real development. Filter by API, severity, tag, or free text. ALWAYS check this before writing Garmin code.",
    {
      query: z.string().optional().describe("Free text search across all gotcha fields"),
      api: z.string().optional().describe("Filter by API/module (e.g. 'Sensor', 'FitContributor', 'WatchUi')"),
      severity: z.enum(["critical", "high", "medium", "low"]).optional().describe("Filter by minimum severity"),
      tag: z.string().optional().describe("Filter by tag (e.g. 'fit', 'sensor', 'type-system')"),
    },
    async ({ query, api, severity, tag }) => {
      reloadGotchasIfChanged();  // pick up edits to gotchas.json without a restart
      let filtered = [...gotchas];

      if (api) {
        const apiLower = api.toLowerCase();
        filtered = filtered.filter(g => g.api.toLowerCase().includes(apiLower));
      }

      if (tag) {
        const tagLower = tag.toLowerCase();
        filtered = filtered.filter(g => g.tags.some(t => t.toLowerCase().includes(tagLower)));
      }

      if (severity) {
        const levels = ["critical", "high", "medium", "low"];
        const minIdx = levels.indexOf(severity);
        filtered = filtered.filter(g => levels.indexOf(g.severity) <= minIdx);
      }

      if (query) {
        const q = query.toLowerCase();
        filtered = filtered.filter(g =>
          g.title.toLowerCase().includes(q) ||
          g.wrong.toLowerCase().includes(q) ||
          g.correct.toLowerCase().includes(q) ||
          (g.codeWrong?.toLowerCase().includes(q) ?? false) ||
          (g.codeCorrect?.toLowerCase().includes(q) ?? false) ||
          g.tags.some(t => t.toLowerCase().includes(q))
        );
      }

      if (filtered.length === 0) {
        return { content: [{ type: "text" as const, text: `No gotchas found matching your criteria. Total gotchas in database: ${gotchas.length}` }] };
      }

      const text = `Found ${filtered.length} gotcha(s):\n\n${filtered.map(formatGotcha).join("\n\n---\n\n")}`;
      return { content: [{ type: "text" as const, text }] };
    }
  );

  // 5. garmin_get_device_info — Device capabilities lookup
  server.tool(
    "garmin_get_device_info",
    "Get Garmin device capabilities: buttons, touch support, display specs, and back-button behavior.",
    {
      device: z.string().optional().describe("Device name (e.g. 'venu3', 'fenix7'). Omit to list all devices."),
    },
    async ({ device }) => {
      if (!device) {
        // Group by category so the (50+ device) list stays scannable.
        const byCategory = new Map<string, DeviceInfo[]>();
        for (const d of DEVICE_INFO) {
          const arr = byCategory.get(d.category) ?? [];
          arr.push(d);
          byCategory.set(d.category, arr);
        }
        const sections = [...byCategory.entries()].map(([cat, devices]) => {
          const rows = devices.map(d =>
            `  ${d.name} — ${d.displayType}${d.displaySize ? ` ${d.displaySize}` : ""}, ${d.buttons.length} button(s), touch: ${d.touch}`
          ).join("\n");
          return `${cat}:\n${rows}`;
        }).join("\n\n");
        const noTouch = DEVICE_INFO.filter(d => !d.touch).map(d => d.name);
        return { content: [{ type: "text" as const, text: `Known devices (${DEVICE_INFO.length}):\n\n${sections}\n\nButton-only (no touchscreen): ${noTouch.join(", ")}` }] };
      }

      const lower = device.toLowerCase();
      const info = DEVICE_INFO.find(d =>
        d.name.toLowerCase() === lower ||
        (d.aliases?.some(a => a.toLowerCase() === lower) ?? false)
      )
        // Fall back to a prefix match so partial ids ("fenix8", "fr26") resolve.
        ?? DEVICE_INFO.find(d => d.name.toLowerCase().startsWith(lower));
      if (!info) {
        return { content: [{ type: "text" as const, text: `Device '${device}' not found. Known: ${DEVICE_INFO.map(d => d.name).join(", ")}` }] };
      }

      const parts = [
        `Device: ${info.name}`,
        `Category: ${info.category}`,
        `Display: ${info.displayType}${info.displaySize ? ` ${info.displaySize}` : ""}`,
        `Touch: ${info.touch}`,
        `Buttons: ${info.buttons.join(", ")}`,
        `Back navigation: ${info.backMethod}`,
      ];
      if (info.aliases?.length) parts.push(`Aliases: ${info.aliases.join(", ")}`);
      return { content: [{ type: "text" as const, text: parts.join("\n") }] };
    }
  );
}
