#!/usr/bin/env node
/**
 * SDK HTML doc parser.
 * Reads Garmin Connect IQ SDK HTML documentation and produces a structured JSON index.
 * Not part of the MCP server runtime: run it once (and after SDK upgrades).
 * Writes ~/.cache/ciqkit/sdk-index.json (or $XDG_CACHE_HOME/ciqkit/).
 *
 * Run: npx -p ciqkit-sdk-mcp ciqkit-sdk-parse [sdk-doc-path]
 * or, in a clone: npm run parse-sdk -- [sdk-doc-path]
 */
import * as fs from "node:fs";
import * as path from "node:path";
import * as cheerio from "cheerio";
import { indexPath } from "./paths.js";

// cheerio 1.x no longer re-exports domhandler's AnyNode; any selection will do.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Sel = cheerio.Cheerio<any>;
import type {
  SdkIndex, SdkModule, SdkClass, SdkMethod, SdkParam,
  SdkField, SdkEnum, SdkEnumValue,
} from "./types.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function textOf($: cheerio.CheerioAPI, el: Sel): string {
  return el.text().replace(/\s+/g, " ").trim();
}

function descriptionOf($: cheerio.CheerioAPI, container: Sel): string {
  const div = container.find(".docstring .discussion");
  if (div.length === 0) return "";
  return textOf($, div);
}

function sinceOf($: cheerio.CheerioAPI, container: Sel): string {
  const since = container.find(".since p");
  if (since.length === 0) return "";
  return since.first().text().trim().replace("API Level ", "");
}

function supportedDevicesOf($: cheerio.CheerioAPI, container: Sel): string[] {
  const ul = container.find(".supported_devices");
  if (ul.length === 0) return [];
  const devices: string[] = [];
  ul.find("li").each((_, el) => {
    const name = $(el).text().trim();
    if (name) devices.push(name);
  });
  return devices;
}

// ---------------------------------------------------------------------------
// Parse a single HTML file for a class
// ---------------------------------------------------------------------------

function parseClassHtml(html: string, moduleName: string, className: string): SdkClass {
  const $ = cheerio.load(html);
  const content = $("#content");

  const fullName = `Toybox.${moduleName}.${className}`;

  // Inheritance
  const inheritsEl = content.find(".inheritName");
  const inherits = inheritsEl.length > 0 ? textOf($, inheritsEl) : undefined;

  // Description
  const description = descriptionOf($, content);
  const since = sinceOf($, content);

  // Child classes
  const childClasses: string[] = [];
  content.find(".children .type a").each((_, el) => {
    childClasses.push($(el).text().trim());
  });

  // Enums
  const enums = parseEnums($, content);

  // Instance fields (var)
  const fields: SdkField[] = [];
  content.find("#instance_attr_details dt.details").each((_, el) => {
    const dt = $(el);
    const sig = dt.find("h3.signature");
    const varName = sig.attr("id")?.replace("-var", "") ?? "";
    const typeText = textOf($, sig.find(".type"));
    const desc = descriptionOf($, dt);
    const fldSince = sinceOf($, dt);

    if (varName) {
      fields.push({
        name: varName,
        type: typeText || "Object",
        description: desc,
        since: fldSince,
      });
    }
  });

  // Methods
  const methods = parseMethods($, content);

  return {
    name: className,
    fullName,
    inherits,
    description,
    since,
    methods,
    fields,
    enums,
    childClasses: childClasses.length > 0 ? childClasses : undefined,
  };
}

// ---------------------------------------------------------------------------
// Parse a module-level HTML file (e.g. Activity.html)
// ---------------------------------------------------------------------------

function parseModuleHtml(html: string, moduleName: string): Partial<SdkModule> {
  const $ = cheerio.load(html);
  const content = $("#content");

  const description = descriptionOf($, content);
  const since = sinceOf($, content);

  // Child classes listed in the module page
  const classNames: string[] = [];
  content.find(".children .type a").each((_, el) => {
    classNames.push($(el).text().trim());
  });

  // Enums (modules like Activity have enums directly)
  const enums = parseEnums($, content);

  // Module-level methods
  const methods = parseMethods($, content);

  return { description, since, methods, enums, classes: [] as SdkClass[] };
}

// ---------------------------------------------------------------------------
// Parse enums
// ---------------------------------------------------------------------------

function parseEnums($: cheerio.CheerioAPI, container: Sel): SdkEnum[] {
  const enums: SdkEnum[] = [];

  container.find("dt[id$='-module']").each((_, el) => {
    const dt = $(el);
    const enumName = dt.find("h3").first().text().trim();
    const enumSince = sinceOf($, dt.next("div.tags").length > 0 ? dt.next("div.tags") : dt);

    // The table follows the dt or the dt's sibling div.tags
    let table = dt.nextAll("table.enumTable").first();
    if (table.length === 0) {
      // Sometimes the table is a sibling further down
      table = dt.parent().find(`table.enumTable`).first();
    }

    const values: SdkEnumValue[] = [];
    table.find("tr").each((_, row) => {
      const cells = $(row).find("td.enumTableCell");
      if (cells.length >= 3) {
        values.push({
          name: $(cells[0]).text().trim(),
          value: $(cells[1]).text().trim(),
          since: $(cells[2]).text().trim().replace("API Level ", ""),
          description: cells.length >= 4 ? $(cells[3]).text().trim() : "",
        });
      }
    });

    if (enumName && values.length > 0) {
      enums.push({ name: enumName, since: enumSince, values });
    }
  });

  return enums;
}

// ---------------------------------------------------------------------------
// Parse methods
// ---------------------------------------------------------------------------

function parseMethods($: cheerio.CheerioAPI, container: Sel): SdkMethod[] {
  const methods: SdkMethod[] = [];

  container.find("#instance_method_details .method_details").each((_, el) => {
    const detail = $(el);
    const sig = detail.find("h3.signature");
    const methodId = sig.attr("id") ?? "";
    const methodName = methodId.replace("-instance_function", "");

    // Full signature text
    const sigText = textOf($, sig);

    // Description
    const desc = descriptionOf($, detail);

    // Parameters
    const params: SdkParam[] = [];
    detail.find(".param > li").each((_, pEl) => {
      const paramEl = $(pEl);
      const pName = paramEl.find("> .name").first().text().trim();
      const pType = textOf($, paramEl.find("> .type").first());
      const pDesc = textOf($, paramEl.find("> .inline").first());

      const subParams: SdkParam[] = [];
      paramEl.find("> ul > li").each((_, spEl) => {
        const sp = $(spEl);
        const spName = sp.find("> .name").first().text().trim();
        const spType = textOf($, sp.find("> .type").first());
        const spDesc = textOf($, sp.find("> .inline").first());
        if (spName) {
          subParams.push({ name: spName, type: spType, description: spDesc });
        }
      });

      if (pName) {
        params.push({
          name: pName,
          type: pType,
          description: pDesc,
          ...(subParams.length > 0 ? { subParams } : {}),
        });
      }
    });

    // Return type
    const retEl = detail.find(".return .type");
    const returnType = retEl.length > 0 ? textOf($, retEl.first()) : "Void";

    // Since
    const methodSince = sinceOf($, detail);

    // Throws
    const throwsEl = detail.find(".throws .type");
    const throws = throwsEl.length > 0 ? textOf($, throwsEl.first()) : undefined;

    if (methodName) {
      methods.push({
        name: methodName,
        signature: sigText,
        description: desc,
        params,
        returnType,
        since: methodSince,
        ...(throws ? { throws } : {}),
      });
    }
  });

  return methods;
}

// ---------------------------------------------------------------------------
// Walk the SDK doc directory tree
// ---------------------------------------------------------------------------

function parseSdk(sdkDocPath: string): SdkIndex {
  const toyboxPath = path.join(sdkDocPath, "Toybox");
  if (!fs.existsSync(toyboxPath)) {
    throw new Error(`Toybox directory not found at: ${toyboxPath}`);
  }

  // Detect SDK version from path
  const sdkVersionMatch = sdkDocPath.match(/connectiq-sdk-\w+-(\d+\.\d+\.\d+)/);
  const sdkVersion = sdkVersionMatch ? sdkVersionMatch[1] : "unknown";

  const modules: SdkModule[] = [];
  const entries = fs.readdirSync(toyboxPath, { withFileTypes: true });

  for (const entry of entries) {
    // Module HTML files are directly under Toybox/ (e.g. Activity.html)
    if (entry.isFile() && entry.name.endsWith(".html")) {
      const moduleName = entry.name.replace(".html", "");
      const moduleHtml = fs.readFileSync(path.join(toyboxPath, entry.name), "utf-8");
      const partial = parseModuleHtml(moduleHtml, moduleName);

      // Check for subdirectory with class files
      const classDir = path.join(toyboxPath, moduleName);
      const classes: SdkClass[] = [];

      if (fs.existsSync(classDir) && fs.statSync(classDir).isDirectory()) {
        const classFiles = fs.readdirSync(classDir).filter(f => f.endsWith(".html"));
        for (const classFile of classFiles) {
          const className = classFile.replace(".html", "");
          const classHtml = fs.readFileSync(path.join(classDir, classFile), "utf-8");
          try {
            const parsed = parseClassHtml(classHtml, moduleName, className);
            classes.push(parsed);
          } catch (e) {
            console.warn(`  Warning: Failed to parse ${moduleName}/${className}: ${e}`);
          }
        }
      }

      modules.push({
        name: moduleName,
        fullName: `Toybox.${moduleName}`,
        description: partial.description ?? "",
        since: partial.since ?? "",
        methods: partial.methods ?? [],
        enums: partial.enums ?? [],
        classes,
      });

      const classCount = classes.length;
      const methodCount = classes.reduce((sum, c) => sum + c.methods.length, 0) + (partial.methods?.length ?? 0);
      const enumCount = (partial.enums?.length ?? 0) + classes.reduce((sum, c) => sum + c.enums.length, 0);
      const fieldCount = classes.reduce((sum, c) => sum + c.fields.length, 0);
      console.log(`  ${moduleName}: ${classCount} classes, ${methodCount} methods, ${enumCount} enums, ${fieldCount} fields`);
    }
  }

  return {
    sdkVersion,
    generatedAt: new Date().toISOString(),
    modules,
  };
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

// Numeric semver compare so "10.0.0" sorts above "9.1.0" (a plain string
// sort puts "10" before "9"). Dir names look like connectiq-sdk-mac-9.1.0-<hash>.
function compareSdkVersion(a: string, b: string): number {
  const ver = (s: string): [number, number, number] => {
    const m = s.match(/(\d+)\.(\d+)\.(\d+)/);
    return m ? [+m[1], +m[2], +m[3]] : [0, 0, 0];
  };
  const [a1, a2, a3] = ver(a);
  const [b1, b2, b3] = ver(b);
  return a1 - b1 || a2 - b2 || a3 - b3;
}

function findSdkPath(): string {
  // 1. Explicit CLI arg wins.
  if (process.argv[2]) {
    return process.argv[2];
  }

  // 2. CIQ_HOME points at an SDK root (the documented override in this repo).
  if (process.env.CIQ_HOME) {
    const docDir = path.join(process.env.CIQ_HOME, "doc");
    if (fs.existsSync(path.join(docDir, "Toybox"))) return docDir;
  }

  // 3. Auto-discover the newest installed SDK per platform.
  const home = process.env.HOME ?? process.env.USERPROFILE ?? "";
  const candidates = [
    process.env.APPDATA && path.join(process.env.APPDATA, "Garmin", "ConnectIQ", "Sdks"),     // Windows
    home && path.join(home, "Library", "Application Support", "Garmin", "ConnectIQ", "Sdks"), // macOS
    home && path.join(home, ".Garmin", "ConnectIQ", "Sdks"),                                  // Linux
  ].filter((p): p is string => Boolean(p));

  for (const sdksDir of candidates) {
    if (!fs.existsSync(sdksDir)) continue;
    const sdks = fs.readdirSync(sdksDir)
      .filter(d => d.startsWith("connectiq-sdk-"))
      .sort(compareSdkVersion);  // newest last
    if (sdks.length > 0) {
      return path.join(sdksDir, sdks[sdks.length - 1], "doc");
    }
  }

  throw new Error(
    "SDK not found. Pass the SDK doc path as an argument, set CIQ_HOME, " +
    "or install an SDK via the Connect IQ SDK Manager."
  );
}

function main(): void {
  const sdkDocPath = findSdkPath();
  console.log(`Parsing SDK docs from: ${sdkDocPath}`);

  const index = parseSdk(sdkDocPath);

  const totalClasses = index.modules.reduce((s, m) => s + m.classes.length, 0);
  const totalMethods = index.modules.reduce((s, m) =>
    s + m.methods.length + m.classes.reduce((cs, c) => cs + c.methods.length, 0), 0);
  const totalEnums = index.modules.reduce((s, m) =>
    s + m.enums.length + m.classes.reduce((cs, c) => cs + c.enums.length, 0), 0);
  const totalFields = index.modules.reduce((s, m) =>
    s + m.classes.reduce((cs, c) => cs + c.fields.length, 0), 0);
  const totalEnumValues = index.modules.reduce((s, m) =>
    s + m.enums.reduce((es, e) => es + e.values.length, 0)
    + m.classes.reduce((cs, c) => cs + c.enums.reduce((es, e) => es + e.values.length, 0), 0), 0);

  console.log(`\nSDK ${index.sdkVersion} parsed:`);
  console.log(`  ${index.modules.length} modules`);
  console.log(`  ${totalClasses} classes`);
  console.log(`  ${totalMethods} methods`);
  console.log(`  ${totalFields} fields`);
  console.log(`  ${totalEnums} enum groups (${totalEnumValues} total values)`);

  const outPath = indexPath();
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, JSON.stringify(index, null, 2));
  console.log(`\nWritten to: ${outPath}`);
}

main();
