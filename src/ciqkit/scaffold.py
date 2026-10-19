"""``ciqkit new``: create a Connect IQ project from a packaged template.

Stdlib only and silent (the CLI wrapper prints). Templates live under
``ciqkit/templates/<name>/`` as ordinary files with ``{{PLACEHOLDERS}}`` in
their text and in their file names; see ``templates/README.md``.
"""
from __future__ import annotations

import os
import re
import struct
import uuid
import zlib
from dataclasses import dataclass, field
from importlib import resources

from . import build, init_cmd

TEMPLATES = ("blank", "tictactoe")
TYPES = {"app": "watch-app", "watchface": "watchface", "datafield": "datafield"}
DEFAULT_DEVICE = build.FALLBACK_DEVICE
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Device ids are substituted verbatim into manifest.xml attributes and a TOML
# string, so anything outside the SDK's own id alphabet (venu3, fr955,
# fenix7pro, ...) is refused rather than escaped: a quote there would not be a
# device anyone has.
_DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_LEFTOVER = re.compile(r"\{\{[A-Z_]+\}\}")
# Only these are substituted; PNGs and the like are copied byte for byte.
_TEXT_SUFFIXES = (".xml", ".toml", ".json", ".md", ".mc", ".jungle", ".gitignore", ".py")
_RESERVED_IDS = {"App", "View"}
TICTACTOE_DEVICES = ("venu3", "fr255", "venusq2")


class ScaffoldError(RuntimeError):
    pass


@dataclass
class Result:
    project: str
    template: str
    type: str
    devices: list[str]
    uuid: str
    files: list[str] = field(default_factory=list)
    key: str | None = None
    key_status: str = ""        # existing | created | skipped
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


# ── naming ─────────────────────────────────────────────────────────────────

def validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise ScaffoldError(
            f"invalid project name {name!r}: use letters, digits, '.', '_' or '-' "
            f"(it becomes the directory, the build output name and the class prefix)")


def class_id(name: str) -> str:
    """CamelCase identifier safe as a Monkey C class prefix: ``my-app`` -> ``MyApp``."""
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", name) if p]
    ident = "".join(p[0].upper() + p[1:] for p in parts)
    if not ident or not ident[0].isalpha():
        ident = "App" + ident
    return ident


def display_name(name: str) -> str:
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", name) if p]
    return " ".join(p[0].upper() + p[1:] for p in parts) or name


# ── devices ────────────────────────────────────────────────────────────────

def installed_devices(devices_dir: str | None = None) -> list[str] | None:
    """Device ids installed in the SDK, or None when the directory is absent."""
    ddir = devices_dir if devices_dir is not None else build.sdk_devices_dir()
    if not os.path.isdir(ddir):
        return None
    return sorted(d for d in os.listdir(ddir) if os.path.isdir(os.path.join(ddir, d)))


def choose_devices(requested: list[str] | None, devices_dir: str | None = None
                   ) -> tuple[list[str], list[str]]:
    """``(devices, warnings)``. Unknown devices warn; they never fail."""
    installed = installed_devices(devices_dir)
    warns: list[str] = []
    if requested:
        devs = list(dict.fromkeys(requested))
        for d in devs:
            if not _DEVICE_RE.match(d):
                raise ScaffoldError(f"invalid device id {d!r}: device ids are letters, "
                                    f"digits, '_' and '-' (e.g. venu3, fr255)")
        if installed is None:
            warns.append("no Connect IQ device directory found (is the SDK Manager "
                         "set up?): could not check the devices")
        else:
            for d in devs:
                if d not in installed:
                    warns.append(f"device {d!r} is not installed in the SDK "
                                 f"(install it in the SDK Manager before building)")
        return devs, warns
    if installed is None:
        warns.append(f"no Connect IQ device directory found; defaulting to {DEFAULT_DEVICE}")
        return [DEFAULT_DEVICE], warns
    if DEFAULT_DEVICE in installed:
        return [DEFAULT_DEVICE], warns
    if installed:
        warns.append(f"{DEFAULT_DEVICE} is not installed; using {installed[0]}")
        return [installed[0]], warns
    warns.append(f"no devices installed in the SDK; defaulting to {DEFAULT_DEVICE}")
    return [DEFAULT_DEVICE], warns


# ── rendering ──────────────────────────────────────────────────────────────

def params_for(name: str, *, type_: str, devices: list[str], app_uuid: str,
               dir_name: str | None = None, template: str = "blank") -> dict[str, str]:
    ident = class_id(name)
    if ident in _RESERVED_IDS:
        ident += "Watch"
    disp = display_name(name)
    return {
        "NAME": name,
        "DIR_NAME": dir_name or name,
        "DISPLAY_NAME": disp,
        "ID": ident,
        "UUID": app_uuid,
        "MANIFEST_TYPE": TYPES[type_],
        "PRODUCTS": "\n".join(f'            <iq:product id="{d}"/>' for d in devices),
        "DEVICE": devices[0],
        "DEVICES": ", ".join(devices),
        "HOME_ANCHOR": (f'anchor = "{disp}"' if type_ == "app"
                        else "# anchor = \"text that must be on this screen\""),
        "CIQKIT_BLOCK": init_cmd.CLAUDE_BLOCK,
    }


def render(text: str, params: dict[str, str]) -> str:
    for k, v in params.items():
        text = text.replace("{{" + k + "}}", v)
    left = _LEFTOVER.search(text)
    if left:
        raise ScaffoldError(f"template placeholder {left.group(0)} has no value")
    return text


def _out_name(rel: str, params: dict[str, str]) -> str:
    parts = []
    for part in rel.split("/"):
        part = render(part, params)
        parts.append("." + part[4:] if part.startswith("dot_") else part)
    return "/".join(parts)


def _walk(root) -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []

    def rec(node, prefix: str) -> None:
        for child in sorted(node.iterdir(), key=lambda n: n.name):
            if child.name == "__pycache__" or child.name == ".DS_Store":
                continue
            rel = prefix + child.name
            if child.is_dir():
                rec(child, rel + "/")
            else:
                out.append((rel, child))

    rec(root, "")
    return out


def template_files(template: str, type_: str, params: dict[str, str]
                   ) -> dict[str, bytes]:
    """Rendered ``{relative output path: bytes}`` for a template (no disk writes)."""
    base = resources.files("ciqkit").joinpath("templates", template)
    layers = [(base.joinpath("common"), "")] if template == "blank" else [(base, "")]
    if template == "blank":
        layers.append((base.joinpath(type_), ""))
    files: dict[str, bytes] = {}
    for root, _ in layers:
        for rel, node in _walk(root):
            data = node.read_bytes()
            if rel.endswith(_TEXT_SUFFIXES) or rel.split("/")[-1].startswith("dot_"):
                data = render(data.decode("utf-8"), params).encode("utf-8")
            files[_out_name(rel, params)] = data
    if template == "blank":
        files["resources/drawables/launcher_icon.png"] = launcher_icon_png(70)
    return files


# ── launcher icon (no Pillow in the base install) ──────────────────────────

def launcher_icon_png(size: int = 70) -> bytes:
    """A neutral placeholder icon: a blue ring on a dark disc, as an RGBA PNG.

    Written with ``zlib`` + ``struct`` so ``ciqkit new`` needs no imaging
    library. Anti-aliased by 4x4 supersampling of a signed-distance test.
    """
    ss = 4
    c = size / 2
    r_out, r_in, r_dot = size * 0.5, size * 0.34, size * 0.14
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    dx, dy = x + (sx + 0.5) / ss - c, y + (sy + 0.5) / ss - c
                    d = (dx * dx + dy * dy) ** 0.5
                    if d > r_out:
                        px = (0, 0, 0, 0)
                    elif r_in <= d <= r_in + size * 0.07 or d <= r_dot:
                        px = (0, 170, 255, 255)
                    else:
                        px = (24, 24, 24, 255)
                    # premultiply so edge pixels average without a dark fringe
                    a = px[3]
                    acc[0] += px[0] * a
                    acc[1] += px[1] * a
                    acc[2] += px[2] * a
                    acc[3] += a
            n = ss * ss
            a = acc[3]
            if a:
                row += bytes([acc[0] // a, acc[1] // a, acc[2] // a, a // n])
            else:
                row += bytes(4)
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + chunk(b"IEND", b""))


# ── the command ────────────────────────────────────────────────────────────

def create(name: str, *, parent_dir: str = ".", template: str = "blank",
           type_: str = "app", devices: list[str] | None = None,
           devices_dir: str | None = None, make_key: bool = True) -> Result:
    validate_name(name)
    if template not in TEMPLATES:
        raise ScaffoldError(f"unknown template {template!r} (choose from {', '.join(TEMPLATES)})")
    if type_ not in TYPES:
        raise ScaffoldError(f"unknown type {type_!r} (choose from {', '.join(TYPES)})")
    if template == "tictactoe" and type_ != "app":
        raise ScaffoldError("the tictactoe template is a watch app: use --type app")

    project = os.path.abspath(os.path.join(parent_dir, name))
    if os.path.exists(project) and (not os.path.isdir(project) or os.listdir(project)):
        raise ScaffoldError(f"{project} already exists and is not an empty directory")

    if not devices and template == "tictactoe":
        # Its README and CLAUDE.md are written around exactly these three
        # (round touch, round buttons-only, rectangular); a one-device manifest
        # would contradict both.
        devices = list(TICTACTOE_DEVICES)
    devs, warns = choose_devices(devices, devices_dir)
    app_uuid = str(uuid.uuid4())
    params = params_for(name, type_=type_, devices=devs, app_uuid=app_uuid)
    files = template_files(template, type_, params)

    for rel, data in files.items():
        target = os.path.join(project, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(data)

    res = Result(project, template, type_, devs, app_uuid, sorted(files), warnings=warns)
    if make_key:
        _ensure_key(res)
    return res


def _ensure_key(res: Result) -> None:
    try:
        res.key = build.resolve_dev_key()
        res.key_status = "existing"
        return
    except build.DevKeyMissing:
        pass
    try:
        res.key = build.generate_dev_key()
        res.key_status = "created"
    except (build.BuildError, OSError) as exc:
        res.key_status = "skipped"
        res.warnings.append(f"no developer key created ({exc}); run `ciqkit keygen` "
                            f"before building")
