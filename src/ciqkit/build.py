"""Project build plumbing: the built-in ``monkeyc`` build, the developer key,
and the default device. Stdlib only, no GUI, no stdout (diagnostics are
exceptions or logging), so the base install and CI can import it freely.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

# Used only when nothing else says which device to build for.
FALLBACK_DEVICE = "venu3"
MANIFEST_NS = "http://www.garmin.com/xml/connectiq"
# Project script that wins over the built-in build when present.
PROJECT_BUILD_SCRIPT = "scripts/build.sh"
PROJECT_BUILD_CMD = "./scripts/build.sh -d {device}"


class BuildError(RuntimeError):
    """The build cannot be set up (no SDK, no key, no jungle). Raised, never exit."""


class DevKeyMissing(BuildError):
    pass


# ── developer key ──────────────────────────────────────────────────────────

def default_dev_key_path() -> str:
    """``$XDG_CONFIG_HOME/ciqkit/developer_key.der`` (``~/.config`` if unset)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "ciqkit", "developer_key.der")


def dev_key_path() -> str:
    """Where the developer key lives: ``CIQKIT_DEV_KEY`` overrides the default."""
    override = os.environ.get("CIQKIT_DEV_KEY")
    return os.path.expanduser(override) if override else default_dev_key_path()


def resolve_dev_key() -> str:
    """Path of an existing developer key, or raise :class:`DevKeyMissing`."""
    path = dev_key_path()
    if not os.path.isfile(path):
        raise DevKeyMissing(
            f"no developer key at {path}. Run `ciqkit keygen` to create one "
            f"(or point CIQKIT_DEV_KEY at an existing .der key).")
    return path


def generate_dev_key(path: str | None = None, bits: int = 4096) -> str:
    """Create an RSA PKCS#8 DER key with ``openssl`` at *path* (0600, dir 0700).

    Refuses to overwrite (``FileExistsError``). The intermediate PEM lives only
    in a 0700 temp dir that is removed afterwards, and never touches the final
    path until it is complete, so a failure leaves no half-written key behind.
    ``bits`` exists for tests; the CLI always uses 4096.
    """
    path = path or dev_key_path()
    if os.path.lexists(path):
        raise FileExistsError(f"{path} already exists; refusing to overwrite a key")
    openssl = shutil.which("openssl")
    if not openssl:
        raise BuildError("`openssl` not found on PATH; install it to generate a key")
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ciqkit-key-", dir=parent) as tmp:
        os.chmod(tmp, 0o700)
        pem = os.path.join(tmp, "key.pem")
        der = os.path.join(tmp, "key.der")
        fd = os.open(pem, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            r = subprocess.run([openssl, "genrsa", str(bits)], stdout=fh,
                               stderr=subprocess.PIPE, timeout=300)
        if r.returncode != 0:
            raise BuildError(f"openssl genrsa failed: {r.stderr.decode(errors='replace')[-300:]}")
        r = subprocess.run([openssl, "pkcs8", "-topk8", "-inform", "PEM",
                            "-outform", "DER", "-nocrypt", "-in", pem, "-out", der],
                           stderr=subprocess.PIPE, timeout=60)
        if r.returncode != 0:
            raise BuildError(f"openssl pkcs8 failed: {r.stderr.decode(errors='replace')[-300:]}")
        os.chmod(der, 0o600)
        # link() fails if the target appeared meanwhile: no silent overwrite.
        try:
            os.link(der, path)
        except FileExistsError:
            raise FileExistsError(f"{path} already exists; refusing to overwrite a key") from None
    os.chmod(path, 0o600)
    return path


# ── device ─────────────────────────────────────────────────────────────────

def sdk_devices_dir() -> str:
    """The SDK Manager's device folder, from the same place the rest of ciqkit
    reads it. A copy of the macOS path used to live here, so on Windows doctor
    and ``ciqkit new`` looked under ``~/Library`` and never saw a device."""
    from .sim.preflight import devices_root   # stdlib-only, no cycle
    return devices_root()


def manifest_products(app_dir: str) -> list[str]:
    """``<iq:product id=...>`` ids in manifest order; [] if absent or malformed."""
    try:
        root = ET.parse(os.path.join(app_dir, "manifest.xml")).getroot()  # noqa: S314 -- the user's own project file
    except (OSError, ET.ParseError):
        return []
    return [p.get("id") for p in root.iter(f"{{{MANIFEST_NS}}}product") if p.get("id")]


def default_device(app_dir: str, devices_dir: str | None = None) -> str:
    """First manifest product installed in the SDK, else :data:`FALLBACK_DEVICE`.

    "Installed" = a directory named after the id under the SDK devices dir. When
    the devices dir cannot be read at all we cannot tell, so the manifest's first
    product is used rather than a device the project may not even target.
    """
    products = manifest_products(app_dir)
    ddir = devices_dir if devices_dir is not None else sdk_devices_dir()
    if os.path.isdir(ddir):
        for pid in products:
            if os.path.isdir(os.path.join(ddir, pid)):
                return pid
        return FALLBACK_DEVICE
    return products[0] if products else FALLBACK_DEVICE


# ── build command ──────────────────────────────────────────────────────────

def prg_name(app_dir: str) -> str:
    """Output basename: the project directory's name (the manifest's app name is
    a resource reference like ``@Strings.AppName``, not a usable file name)."""
    return os.path.basename(os.path.abspath(app_dir)) or "app"


def monkeyc_argv(app_dir: str, device: str, *, tests: bool = False,
                 sdk: str | None = None, key: str | None = None) -> list[str]:
    """The built-in build: ``monkeyc -f monkey.jungle -d DEV -y KEY -o bin/NAME.prg``.

    ``tests=True`` adds ``-t`` and writes ``bin/NAME-test.prg`` so a unit-test
    build can never be mistaken for the normal one.
    """
    if sdk is None:
        from .sim.preflight import detect_sdk
        sdk = detect_sdk()
    # The SDK ships bin/monkeyc.bat on Windows, a shell script elsewhere.
    monkeyc = (os.path.join(sdk, "bin", "monkeyc.bat" if sys.platform == "win32"
                            else "monkeyc") if sdk else "")
    if not monkeyc or not os.path.isfile(monkeyc):
        raise BuildError(
            "no Connect IQ SDK with bin/monkeyc found. Install one with the SDK "
            "Manager or set CIQ_HOME (or pass an already-built --prg); "
            "`ciqkit doctor` shows what was searched.")
    if not os.path.isfile(os.path.join(app_dir, "monkey.jungle")):
        raise BuildError(f"no monkey.jungle in {app_dir}. Pass an already-built --prg, "
                         f"set CIQKIT_BUILD_CMD, or add scripts/build.sh")
    key = key or resolve_dev_key()
    out = os.path.join("bin", prg_name(app_dir) + ("-test" if tests else "") + ".prg")
    argv = [monkeyc, "-f", "monkey.jungle", "-d", device, "-y", key, "-o", out]
    if tests:
        argv.append("-t")
    return argv


def toml_build_cmd(app_dir: str) -> str | None:
    """``[build] cmd`` from the project's ``ciqkit.toml`` (None if unset).

    May raise :class:`ciqkit.config.ConfigError` for a malformed file.
    """
    from .config import load_config
    return load_config(app_dir).build_cmd


def toml_test_cmd(app_dir: str) -> str | None:
    """``[build] test_cmd``: a build command that produces the ``-t`` variant."""
    from .config import load_config
    return load_config(app_dir).build_test_cmd


def resolve_device(app_dir: str, explicit: str | None = None) -> str:
    """Device for *app_dir*: explicit (CLI/MCP arg) > ``[sim] device`` > default.

    There is no device environment variable, so that rung of the usual
    flag > env > toml > default ladder does not exist.
    """
    if explicit:
        return explicit
    from .config import load_config
    return load_config(app_dir).device or default_device(app_dir)


def build_route(app_dir: str, build_cmd: str | None = None) -> str:
    """Which route :func:`resolve_build` takes, as a label (for ``doctor``)."""
    if build_cmd:
        return "explicit command"
    if os.environ.get("CIQKIT_BUILD_CMD"):
        return "env (CIQKIT_BUILD_CMD)"
    if toml_build_cmd(app_dir):
        return "toml ([build] cmd)"
    if os.path.isfile(os.path.join(app_dir, PROJECT_BUILD_SCRIPT)):
        return "build.sh (scripts/build.sh)"
    return "built-in monkeyc"


def resolve_build(app_dir: str, device: str, *, build_cmd: str | None = None,
                  tests: bool = False) -> tuple[list[str] | None, str]:
    """Pick the build for *app_dir*.

    Returns ``(argv, shell_template)``; exactly one is meaningful. A user-chosen
    command (explicit arg, ``CIQKIT_BUILD_CMD``, ``[build] cmd``, or an existing
    ``scripts/build.sh``) comes back as a template for the caller to
    ``.format(device=...)`` and ``shlex.split``; otherwise the built-in
    ``monkeyc`` argv is returned.
    """
    if tests and not build_cmd:
        # `[build] test_cmd` exists precisely so a custom-build project can
        # produce the -t variant; it outranks the normal-build routes below,
        # which have no way to say "with tests".
        test_cmd = toml_test_cmd(app_dir)
        if test_cmd:
            return None, test_cmd
    cmd = build_cmd or os.environ.get("CIQKIT_BUILD_CMD") or toml_build_cmd(app_dir)
    if cmd:
        return None, cmd
    if os.path.isfile(os.path.join(app_dir, PROJECT_BUILD_SCRIPT)):
        return None, PROJECT_BUILD_CMD
    return monkeyc_argv(app_dir, device, tests=tests), ""
