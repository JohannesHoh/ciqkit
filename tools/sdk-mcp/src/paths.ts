import * as os from "node:os";
import * as path from "node:path";

/**
 * Where the locally generated SDK index lives: `$XDG_CACHE_HOME/ciqkit/` or
 * `~/.cache/ciqkit/`. A per-user cache rather than the package directory, so
 * an `npx`/global install (read-only, replaced on every upgrade) can still
 * hold an index that is generated once from the user's own SDK install.
 */
export function cacheDir(): string {
  const xdg = process.env.XDG_CACHE_HOME;
  const base = xdg && path.isAbsolute(xdg) ? xdg : path.join(os.homedir(), ".cache");
  return path.join(base, "ciqkit");
}

export function indexPath(): string {
  return path.join(cacheDir(), "sdk-index.json");
}
