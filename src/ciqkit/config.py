"""Optional per-project ``ciqkit.toml``.

Stdlib only, no GUI, no stdout (problems are exceptions or ``logging``), so the
base install and CI can import it freely. See docs/config.md for the schema.

TOML reader: ``tomllib`` on 3.11+; on 3.10 ``tomli`` if it happens to be
installed, otherwise the small reader below. The base install has no
dependencies on purpose, so the fallback reader exists rather than a ``tomli``
requirement. It supports exactly the subset the schema needs: comments,
``[table]`` and ``[[array]]`` headers (dotted names), strings (basic, literal,
and their triple-quoted forms), integers, floats, booleans, arrays and inline
tables. Anything else is a :class:`ConfigError` naming the line, never a
silent misread.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

log = logging.getLogger("ciqkit.config")

CONFIG_NAME = "ciqkit.toml"

_KNOWN = {
    "build": {"cmd", "test_cmd"},
    "sim": {"device"},
}
_KNOWN_SCREEN = {"name", "steps", "anchor", "seed", "storage"}
_TOP = {"build", "sim", "screens"}


class ConfigError(ValueError):
    """``ciqkit.toml`` is unreadable, malformed or has a value of the wrong type.

    Raised, never ``sys.exit``: library callers (the scheduler thread, the MCP
    server) must be able to report it as an ordinary failure.
    """


@dataclass
class Screen:
    """One named screen: exactly the arguments of a saved ``sim shot``/``nav``."""
    name: str
    steps: list = field(default_factory=list)       # --steps (JSON gesture list)
    anchor: list = field(default_factory=list)      # --anchor, repeatable
    seed: dict = field(default_factory=dict)        # --seed NAME=VALUE
    storage: str | None = None                      # --storage fresh|keep|DIR


@dataclass
class Config:
    path: str | None = None            # None: no ciqkit.toml present
    build_cmd: str | None = None
    build_test_cmd: str | None = None
    device: str | None = None
    screens: list = field(default_factory=list)

    def screen(self, name: str) -> Screen | None:
        for s in self.screens:
            if s.name == name:
                return s
        return None


# ── minimal TOML subset reader (3.10 without tomli) ────────────────────────

_BARE = re.compile(r"[A-Za-z0-9_-]+")
_NUM = re.compile(r"[+-]?(?:\d[\d_]*)(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?")
_ESC = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r",
        '"': '"', "\\": "\\"}


class _Reader:
    def __init__(self, text: str):
        self.s = text
        self.i = 0

    def line(self) -> int:
        return self.s.count("\n", 0, self.i) + 1

    def err(self, msg: str):
        raise ConfigError(f"line {self.line()}: {msg}")

    def peek(self) -> str:
        return self.s[self.i] if self.i < len(self.s) else ""

    def skip_ws(self, newlines: bool = False):
        while self.i < len(self.s):
            c = self.s[self.i]
            if c in " \t" or (newlines and c in "\r\n"):
                self.i += 1
            elif c == "#":
                while self.i < len(self.s) and self.s[self.i] != "\n":
                    self.i += 1
            else:
                break

    def end_of_line(self):
        self.skip_ws()
        if self.peek() not in ("", "\n", "\r"):
            self.err(f"unexpected {self.peek()!r} after value")

    def key(self) -> list:
        parts = []
        while True:
            self.skip_ws()
            c = self.peek()
            if c in ('"', "'"):
                parts.append(self.string())
            else:
                m = _BARE.match(self.s, self.i)
                if not m:
                    self.err("expected a key")
                parts.append(m.group())
                self.i = m.end()
            self.skip_ws()
            if self.peek() == ".":
                self.i += 1
                continue
            return parts

    def string(self) -> str:
        q = self.s[self.i]
        triple = self.s.startswith(q * 3, self.i)
        self.i += 3 if triple else 1
        if triple and self.peek() == "\n":
            self.i += 1
        out = []
        while self.i < len(self.s):
            c = self.s[self.i]
            if triple and self.s.startswith(q * 3, self.i):
                self.i += 3
                return "".join(out)
            if not triple and c == q:
                self.i += 1
                return "".join(out)
            if c == "\n" and not triple:
                self.err("unterminated string")
            if c == "\\" and q == '"':
                n = self.s[self.i + 1:self.i + 2]
                if n in _ESC:
                    out.append(_ESC[n])
                    self.i += 2
                    continue
                if n in ("u", "U"):
                    width = 4 if n == "u" else 8
                    hexs = self.s[self.i + 2:self.i + 2 + width]
                    try:
                        out.append(chr(int(hexs, 16)))
                    except ValueError:
                        self.err("bad unicode escape")
                    self.i += 2 + width
                    continue
                self.err(f"unsupported escape \\{n}")
            out.append(c)
            self.i += 1
        self.err("unterminated string")

    def value(self):
        self.skip_ws()
        c = self.peek()
        if c in ('"', "'"):
            return self.string()
        if c == "[":
            self.i += 1
            arr = []
            while True:
                self.skip_ws(newlines=True)
                if self.peek() == "]":
                    self.i += 1
                    return arr
                arr.append(self.value())
                self.skip_ws(newlines=True)
                if self.peek() == ",":
                    self.i += 1
                elif self.peek() != "]":
                    self.err("expected ',' or ']' in array")
        if c == "{":
            self.i += 1
            tbl: dict = {}
            self.skip_ws()
            if self.peek() == "}":
                self.i += 1
                return tbl
            while True:
                path = self.key()
                self.skip_ws()
                if self.peek() != "=":
                    self.err("expected '=' in inline table")
                self.i += 1
                _assign(tbl, path, self.value(), self)
                self.skip_ws()
                if self.peek() == ",":
                    self.i += 1
                elif self.peek() == "}":
                    self.i += 1
                    return tbl
                else:
                    self.err("expected ',' or '}' in inline table")
        for lit, val in (("true", True), ("false", False)):
            if self.s.startswith(lit, self.i):
                self.i += len(lit)
                return val
        m = _NUM.match(self.s, self.i)
        if m and m.group():
            self.i = m.end()
            raw = m.group().replace("_", "")
            return float(raw) if re.search(r"[.eE]", raw) else int(raw)
        self.err(f"unsupported value starting {self.s[self.i:self.i + 12]!r}")


def _assign(tbl: dict, path: list, val, rd: _Reader):
    for p in path[:-1]:
        tbl = tbl.setdefault(p, {})
        if not isinstance(tbl, dict):
            rd.err(f"{p!r} is not a table")
    if path[-1] in tbl:
        rd.err(f"duplicate key {path[-1]!r}")
    tbl[path[-1]] = val


def _descend(root: dict, path: list, rd: _Reader, *, array_leaf: bool) -> dict:
    cur = root
    for n, p in enumerate(path):
        last = n == len(path) - 1
        if last and array_leaf:
            lst = cur.setdefault(p, [])
            if not isinstance(lst, list):
                rd.err(f"{p!r} is not an array of tables")
            lst.append({})
            return lst[-1]
        nxt = cur.setdefault(p, {})
        if isinstance(nxt, list):
            nxt = nxt[-1]
        if not isinstance(nxt, dict):
            rd.err(f"{p!r} is not a table")
        cur = nxt
    return cur


def parse_minimal(text: str) -> dict:
    """Parse the documented TOML subset. Raises :class:`ConfigError`."""
    rd = _Reader(text)
    root: dict = {}
    cur = root
    while True:
        rd.skip_ws(newlines=True)
        if rd.i >= len(rd.s):
            return root
        if rd.peek() == "[":
            array = rd.s.startswith("[[", rd.i)
            rd.i += 2 if array else 1
            path = rd.key()
            closer = "]]" if array else "]"
            if not rd.s.startswith(closer, rd.i):
                rd.err(f"expected {closer!r}")
            rd.i += len(closer)
            rd.end_of_line()
            cur = _descend(root, path, rd, array_leaf=array)
        else:
            path = rd.key()
            rd.skip_ws()
            if rd.peek() != "=":
                rd.err("expected '=' after key")
            rd.i += 1
            _assign(cur, path, rd.value(), rd)
            rd.end_of_line()


def _loads(text: str, *, force_fallback: bool = False) -> dict:
    if not force_fallback:
        try:
            import tomllib as _t
        except ImportError:
            try:
                import tomli as _t   # type: ignore[no-redef]
            except ImportError:
                _t = None
        if _t is not None:
            try:
                return _t.loads(text)
            except _t.TOMLDecodeError as exc:
                raise ConfigError(str(exc)) from None
    return parse_minimal(text)


# ── schema ─────────────────────────────────────────────────────────────────

def _warn_unknown(where: str, got, known, path: str):
    for k in got:
        if k not in known:
            log.warning("%s: unknown key %s%s (ignored)", path, where, k)


def _str(v, where: str, path: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ConfigError(f"{path}: {where} must be a non-empty string")
    return v


def _str_list(v, where: str, path: str) -> list:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ConfigError(f"{path}: {where} must be a string or a list of strings")
    return list(v)


def _parse_seed(v, where: str, path: str) -> dict:
    """``["NAME=VALUE", ...]`` (the CLI's ``--seed`` form) or ``{NAME = VALUE}``."""
    if isinstance(v, dict):
        return {str(k): (str(x).lower() if isinstance(x, bool) else str(x))
                for k, x in v.items()}
    out = {}
    for kv in _str_list(v, where, path):
        k, sep, val = kv.partition("=")
        if not sep or not k.strip():
            raise ConfigError(f"{path}: {where} entries must be NAME=VALUE, got {kv!r}")
        out[k.strip()] = val.strip()
    return out


def _parse_screen(raw, n: int, path: str) -> Screen:
    where = f"screens[{n}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: {where} must be a table")
    _warn_unknown(f"{where}.", raw, _KNOWN_SCREEN, path)
    name = _str(raw.get("name"), f"{where}.name", path)
    if name != os.path.basename(name) or name in (".", ".."):
        raise ConfigError(f"{path}: {where}.name {name!r} must be a plain file "
                          f"name (it becomes <name>.png)")
    steps = raw.get("steps", [])
    if not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
        raise ConfigError(f"{path}: {where}.steps must be a list of gesture tables")
    storage = raw.get("storage")
    if storage is not None:
        storage = _str(storage, f"{where}.storage", path)
    return Screen(name=name, steps=steps,
                  anchor=_str_list(raw.get("anchor", []), f"{where}.anchor", path),
                  seed=_parse_seed(raw.get("seed", []), f"{where}.seed", path),
                  storage=storage)


def parse_config(data: dict, path: str = CONFIG_NAME) -> Config:
    """Validate a parsed document into a :class:`Config`."""
    _warn_unknown("", data, _TOP, path)
    cfg = Config(path=path)
    for tbl in ("build", "sim"):
        sect = data.get(tbl, {})
        if not isinstance(sect, dict):
            raise ConfigError(f"{path}: [{tbl}] must be a table")
        _warn_unknown(f"{tbl}.", sect, _KNOWN[tbl], path)
    b, s = data.get("build", {}), data.get("sim", {})
    if "cmd" in b:
        cfg.build_cmd = _str(b["cmd"], "build.cmd", path)
    if "test_cmd" in b:
        cfg.build_test_cmd = _str(b["test_cmd"], "build.test_cmd", path)
    if "device" in s:
        cfg.device = _str(s["device"], "sim.device", path)
    raw_screens = data.get("screens", [])
    if not isinstance(raw_screens, list):
        raise ConfigError(f"{path}: screens must be an array of tables ([[screens]])")
    seen = set()
    for n, raw in enumerate(raw_screens):
        sc = _parse_screen(raw, n, path)
        if sc.name in seen:
            raise ConfigError(f"{path}: duplicate screen name {sc.name!r}")
        seen.add(sc.name)
        cfg.screens.append(sc)
    return cfg


def load_config(app_dir: str, *, force_fallback: bool = False) -> Config:
    """The project's ``ciqkit.toml``, or an empty :class:`Config` if there is none.

    Read fresh each call (the file is tiny and the daemon outlives edits).
    Raises :class:`ConfigError` naming the file when it is malformed.
    """
    path = os.path.join(os.path.abspath(app_dir), CONFIG_NAME)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return Config()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from None
    try:
        data = _loads(text, force_fallback=force_fallback)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    return parse_config(data, path)
