"""``ciqkit init``: wire an existing Connect IQ project up for ciqkit.

Plans a set of file changes and either returns them (dry run, the default) or
applies them. Stdlib only and silent: the CLI wrapper (:mod:`ciqkit.init_cli`)
prints. Two rules shape everything here:

* Nothing is overwritten silently. An existing ``ciqkit.toml`` or workflow is
  left alone and reported; JSON is merged, never replaced; text files only ever
  gain a marked block or missing lines.
* The plan is computed from the files as they are, then written. Re-running
  after ``--write`` therefore plans nothing (idempotent).
"""
from __future__ import annotations

import difflib
import json
import os
from dataclasses import dataclass, field

from . import build

BLOCK_BEGIN = "<!-- ciqkit:begin -->"
BLOCK_END = "<!-- ciqkit:end -->"

CLAUDE_BLOCK = f"""{BLOCK_BEGIN}
## Working on this Connect IQ project with ciqkit

Managed by `ciqkit init`; edit outside the markers, not inside.

* **Verify every UI change on the simulator.** A build that compiles is not a
  screen that looks right. After changing anything drawn or any input handling:
  `ciqkit sim shot NAME --native --anchor "TEXT THAT MUST BE ON SCREEN"`, then
  look at the PNG. An anchor that is not found means you are not on the screen
  you think you are. Screens listed in `ciqkit.toml` regenerate with
  `ciqkit sim shots --all --native --out-dir shots`; check them with
  `ciqkit layout shots` (add `--rect` for rectangular devices).
* **Run the unit tests** with `ciqkit sim test`.
* **Look APIs up, do not guess them.** Use the `garmin-sdk` MCP server
  (`garmin_lookup_api`, `garmin_search_sdk`, `garmin_get_device_info`). Before
  writing input, storage or drawing code, read the pitfalls with
  `garmin_get_gotchas`.
* Never commit a framed screenshot (one taken without `--native`): it contains
  the device image from Garmin's SDK. Never commit a developer key.
* `ciqkit doctor --project .` explains a build or simulator problem.
{BLOCK_END}
"""

MCP_SERVERS = {
    "ciqsim": {"command": "ciqkit-mcp"},
    # WP8 publishes this as an npm package; until then the repo's own
    # tools/sdk-mcp/scripts/run-server.sh does the same job.
    "garmin-sdk": {"command": "npx", "args": ["-y", "ciqkit-sdk-mcp"]},
}
# Names a user may already have given the SDK server; do not add a second one.
SDK_SERVER_ALIASES = ("garmin-sdk", "sdk-mcp", "ciqkit-sdk-mcp")

GITIGNORE_ENTRIES = ["bin/", "*.prg", "*.iq", "*.der", "*.pem", "*.jks", "shots/"]
_GITIGNORE_EQUIV = {"bin/": ("bin", "/bin", "/bin/"), "shots/": ("shots", "/shots", "/shots/")}

WORKFLOW_PATH = ".github/workflows/ciqkit-checks.yml"
WORKFLOW = """\
# Static checks on committed baseline screenshots. No simulator needed: the
# pictures are captured locally with `ciqkit sim shots --all --native` and
# committed under baselines/ (one PNG per screen; never framed ones).
name: ciqkit checks

on:
  push:
  pull_request:

jobs:
  screens:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install "ciqkit[checks]"
      - name: Layout and image quality of the baselines
        run: |
          if [ -d baselines ]; then
            ciqkit layout --fail-under 80 baselines
            ciqkit shot-score --fail-under 70 baselines
          else
            echo "no baselines/ directory yet; nothing to check"
          fi
"""


@dataclass
class Change:
    path: str                 # relative to the project
    action: str               # create | modify | skip
    new: str | None = None    # full new content (create/modify)
    old: str | None = None
    note: str = ""
    # A skip the user must act on: the file could not be merged safely, so the
    # project is NOT fully wired up. Makes the CLI exit non-zero.
    attention: bool = False

    def diff(self) -> str:
        if self.new is None:
            return ""
        a = (self.old or "").splitlines(keepends=True)
        b = self.new.splitlines(keepends=True)
        return "".join(difflib.unified_diff(
            a, b, fromfile="/dev/null" if self.old is None else f"a/{self.path}",
            tofile=f"b/{self.path}"))

    def to_dict(self) -> dict:
        return {"path": self.path, "action": self.action, "note": self.note,
                "attention": self.attention}


@dataclass
class Plan:
    project: str
    changes: list[Change] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def pending(self) -> list[Change]:
        return [c for c in self.changes if c.action in ("create", "modify")]

    @property
    def attention(self) -> list[Change]:
        return [c for c in self.changes if c.attention]


class InitError(RuntimeError):
    pass


def _escapes(project: str, rel: str) -> bool:
    """True if writing *rel* would land outside *project*, or go through a
    symlink at all.

    ``init`` runs in checkouts you did not write. A ``CLAUDE.md`` or
    ``.gitignore`` that is a symlink to ``~/.zshrc`` (or a ``.github`` that
    points elsewhere) would otherwise have ciqkit append to a file outside the
    project, and a dangling one would have it create that file.
    """
    root = os.path.realpath(project)
    cur = root
    for part in rel.split("/"):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            return True
    target = os.path.realpath(os.path.join(project, rel))
    return os.path.commonpath([root, target]) != root


def _symlink_skip(project: str, rel: str) -> Change | None:
    if _escapes(project, rel):
        return Change(rel, "skip", attention=True,
                      note="is (or is under) a symlink; not followed, left "
                           "untouched -- add the ciqkit parts by hand")
    return None


def _toml_str(value: str) -> str:
    """A TOML basic string. JSON string escaping is a valid subset of it."""
    return json.dumps(value)


def _read(path: str) -> str | None:
    # newline="": keep the file's own line endings, so a CRLF file is not
    # rewritten to LF outside the lines ciqkit adds.
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


# ── individual files ───────────────────────────────────────────────────────

def detect_build(project: str) -> tuple[str | None, str]:
    """``(cmd_template_or_None, human description)`` of a custom build, if any.

    Only ``scripts/build.sh`` has a known calling convention (``-d DEVICE``,
    ciqkit's own default), so only it yields an active command. Anything else is
    returned as a *commented* suggestion by the caller: a wrong guess would turn
    a working project into a failing build.
    """
    if os.path.isfile(os.path.join(project, build.PROJECT_BUILD_SCRIPT)):
        return build.PROJECT_BUILD_CMD, "scripts/build.sh"
    for name, cmd in (("build.sh", "./build.sh -d {device}"),
                      ("Makefile", "make DEVICE={device}"),
                      ("makefile", "make DEVICE={device}"),
                      ("build.py", "python3 build.py --device {device}"),
                      ("justfile", "just build {device}")):
        if os.path.isfile(os.path.join(project, name)):
            return "?" + cmd, name
    return None, ""


def _plan_toml(project: str) -> Change:
    path = "ciqkit.toml"
    if os.path.lexists(os.path.join(project, path)):
        return Change(path, "skip", note="exists; left untouched (see docs/config.md)")
    device = build.default_device(project)
    lines = ["# ciqkit settings for this project. Schema: docs/config.md in the ciqkit repo.", ""]
    cmd, what = detect_build(project)
    if cmd and not cmd.startswith("?"):
        lines += [f"# Detected {what}; ciqkit would use it anyway, stated here so it is visible.",
                  "# `ciqkit sim test` needs a build that produces the `monkeyc -t` variant:",
                  "# add test_cmd = \"...\" (or pass --prg) before relying on it.",
                  "[build]", f"cmd = {_toml_str(cmd)}", ""]
    elif cmd:
        lines += [f"# Detected {what}, but its arguments are a guess. Check, uncomment, and",
                  "# add test_cmd for `ciqkit sim test`. Without [build], ciqkit uses its",
                  "# built-in monkeyc build.",
                  "# [build]", f"# cmd = {_toml_str(cmd[1:])}", ""]
    lines += ["[sim]", f"device = {_toml_str(device)}", "",
              "# Named screens: `ciqkit sim shots --all` regenerates every one of them.",
              "[[screens]]", 'name = "home"', ""]
    return Change(path, "create", new="\n".join(lines),
                  note=f"[sim] device = {device}" + (f"; build: {what}" if what else ""))


def _has_sdk_server(servers: dict) -> bool:
    """An SDK server already registered, under one of the usual names or under
    any name whose command line runs it."""
    if any(a in servers for a in SDK_SERVER_ALIASES):
        return True
    for spec in servers.values():
        if not isinstance(spec, dict):
            continue
        args = spec.get("args") if isinstance(spec.get("args"), list) else []
        words = [str(spec.get("command", ""))] + [str(a) for a in args]
        if any("ciqkit-sdk-mcp" in w or "sdk-mcp/" in w for w in words):
            return True
    return False


def merge_mcp(old_text: str | None) -> tuple[str | None, str]:
    """``(new_text_or_None, note)``. ``None`` = nothing to do or cannot merge.

    A note starting with ``!`` means "could not merge": the caller flags it.
    """
    if old_text is None or not old_text.strip():
        data: dict = {}
    else:
        try:
            data = json.loads(old_text)
        except ValueError as exc:
            return None, f"!not valid JSON ({exc}); left untouched, add the servers by hand"
        if not isinstance(data, dict):
            return None, "!top level is not an object; left untouched, add the servers by hand"
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return None, "!`mcpServers` is not an object; left untouched, add the servers by hand"
    added = []
    if "ciqsim" not in servers:
        servers["ciqsim"] = MCP_SERVERS["ciqsim"]
        added.append("ciqsim")
    if not _has_sdk_server(servers):
        servers["garmin-sdk"] = MCP_SERVERS["garmin-sdk"]
        added.append("garmin-sdk")
    if not added:
        return None, "already has ciqsim and an SDK server"
    return json.dumps(data, indent=2) + "\n", "adds " + ", ".join(added)


def _plan_mcp(project: str) -> Change:
    path = ".mcp.json"
    old = _read(os.path.join(project, path))
    new, note = merge_mcp(old)
    if new is None:
        return Change(path, "skip", note=note.lstrip("!"), attention=note.startswith("!"))
    return Change(path, "create" if old is None else "modify", new=new, old=old, note=note)


class MarkerError(ValueError):
    """CLAUDE.md has a begin marker with no end marker after it."""


def apply_block(text: str | None, block: str = CLAUDE_BLOCK) -> str:
    """Insert or replace the marked block; bytes outside the markers never change.

    The end marker is searched AFTER the begin marker, so a stray end marker
    earlier in the file cannot make every run append another block. A begin
    marker with no end after it raises :class:`MarkerError`: replacing "to the
    end of the file" or "up to the next block" would delete the user's text.
    """
    if not text:
        return "# CLAUDE.md\n\n" + block
    b = text.find(BLOCK_BEGIN)
    e = text.find(BLOCK_END, b) if b != -1 else -1
    if b != -1 and e == -1:
        raise MarkerError(f"{BLOCK_BEGIN} has no {BLOCK_END} after it")
    nl = "\r\n" if "\r\n" in text else "\n"
    block = block.replace("\n", nl)
    if b != -1:
        e += len(BLOCK_END)
        # Swallow the single newline that ends the old block so the block's own
        # trailing newline does not accumulate a blank line per run.
        if text.startswith(nl, e):
            e += len(nl)
        return text[:b] + block + text[e:]
    sep = "" if text.endswith(nl * 2) else (nl if text.endswith(nl) else nl * 2)
    return text + sep + block


def _plan_claude(project: str) -> Change:
    path = "CLAUDE.md"
    old = _read(os.path.join(project, path))
    try:
        new = apply_block(old)
    except MarkerError as exc:
        return Change(path, "skip", attention=True,
                      note=f"{exc}; left untouched -- fix or remove the marker")
    if new == old:
        return Change(path, "skip", note="ciqkit block already current")
    return Change(path, "create" if old is None else "modify", new=new, old=old,
                  note="adds the ciqkit block" if old is None or BLOCK_BEGIN not in old
                  else "updates the ciqkit block")


def _plan_gitignore(project: str) -> Change:
    path = ".gitignore"
    old = _read(os.path.join(project, path))
    have = {ln.strip() for ln in (old or "").splitlines()}
    missing = [e for e in GITIGNORE_ENTRIES
               if e not in have and not any(a in have for a in _GITIGNORE_EQUIV.get(e, ()))]
    if not missing:
        return Change(path, "skip", note="already ignores build output and keys")
    base = old or ""
    nl = "\r\n" if "\r\n" in base else "\n"
    if base and not base.endswith("\n"):
        base += nl
    new = base + (nl if base else "") + "# ciqkit" + nl + nl.join(missing) + nl
    return Change(path, "create" if old is None else "modify", new=new, old=old,
                  note="adds " + ", ".join(missing))


def _plan_workflow(project: str) -> Change:
    if os.path.lexists(os.path.join(project, WORKFLOW_PATH)):
        return Change(WORKFLOW_PATH, "skip", note="exists; left untouched")
    return Change(WORKFLOW_PATH, "create", new=WORKFLOW,
                  note="layout + shot-score on baselines/ (create baselines/ with "
                       "`ciqkit sim shots --all --native --out-dir baselines`)")


# ── public API ─────────────────────────────────────────────────────────────

def plan(project: str, *, ci: bool = False) -> Plan:
    project = os.path.abspath(project)
    if not os.path.isdir(project):
        raise InitError(f"{project} is not a directory")
    if not os.path.isfile(os.path.join(project, "manifest.xml")):
        raise InitError(f"no manifest.xml in {project}: not a Connect IQ project. "
                        f"Use `ciqkit new` to start one.")
    p = Plan(project)
    if not os.path.isfile(os.path.join(project, "monkey.jungle")):
        p.notes.append("no monkey.jungle: the built-in build needs one "
                       "(a custom build command may not)")
    products = build.manifest_products(project)
    if not products:
        p.notes.append("manifest.xml lists no products")
    planners = [("ciqkit.toml", _plan_toml), (".mcp.json", _plan_mcp),
                ("CLAUDE.md", _plan_claude), (".gitignore", _plan_gitignore)]
    if ci:
        planners.append((WORKFLOW_PATH, _plan_workflow))
    for rel, planner in planners:
        p.changes.append(_symlink_skip(project, rel) or planner(project))
    return p


def write(p: Plan) -> list[str]:
    """Apply the pending changes; returns the relative paths written."""
    done = []
    for c in p.pending:
        if _escapes(p.project, c.path):
            # Re-checked at write time: the plan may be older than the tree.
            raise InitError(f"refusing to write {c.path}: it is (or is under) a symlink")
        target = os.path.join(p.project, c.path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="") as fh:
            fh.write(c.new or "")
        done.append(c.path)
    return done
