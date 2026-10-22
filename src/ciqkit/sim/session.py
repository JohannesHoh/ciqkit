"""SimSession -- the single canonical driver of the Connect IQ Simulator.

Only the broker constructs one, so exactly one actor ever touches the GUI. It
wraps the screen-capture primitives in :mod:`ciqkit.capture.screenshot_utils`
and adds:

  * load verification, so we can always tell the loaded app from the device
    watchface, a blank launcher, or Connect IQ's own loading screen;
  * verified input -- taps, swipes and keys that confirm the screen actually
    changed (via a brightness signature and/or an OCR anchor) instead of firing
    blind coordinates and hoping;
  * constant seeding with a byte-exact guaranteed restore and a git-clean
    check, so an abnormal exit cannot leave a patch in your working tree;
  * a small structured test runner.

Everything here is synchronous and single-threaded; concurrency + priority live
in the broker, not here.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

from .. import build as _build
from .._compat import hidden_console_kwargs
from ..capture import screenshot_utils as su
from ..paths import default_out_dir
from . import console
from . import ocr
from . import preflight
from . import seedjournal
from . import simport
from . import storage
from .preflight import detect_sdk
from .protocol import DEFAULT_PLAYBACK_MS as _DEFAULT_PLAYBACK_MS
from .protocol import DEFAULT_SHOT_SETTLE_S, button_id, swipe_spec


log = logging.getLogger("ciqkit.sim")

# device id -> face px read from the installed SDK profile (None = not there)
_SDK_RES_CACHE: dict = {}


def _with_dismissed(fn):
    """Attach what the dialog/window dismisser did during ``fn`` to its result.

    A crashed simulator process or a closed aux window is evidence, not noise:
    it is reported as ``dismissed`` on the step/shot result so a crash is never
    silent (and a crash dialog does not hide behind an ok:true).
    """
    import functools

    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        su.drain_dismissed()                 # events from before this call
        try:
            r = fn(self, *a, **kw)
        finally:
            ev = su.drain_dismissed()
        if ev and isinstance(r, dict):
            r["dismissed"] = ev
            if any(e.get("kind") == "simulator_crash_dialog" for e in ev):
                r["simulator_crashed"] = True
        return r
    return wrapper


class SimError(RuntimeError):
    pass


# Where build scripts in the wild drop their .prg, searched in this order when
# the build's own output does not name the file.
BUILD_OUTPUT_DIRS = ("bin", "build", "gen", ".")


def _announced_prg(output: str, app_dir: str, since: float) -> str | None:
    """The .prg path a build script printed, or None.

    Build scripts announce their output in whatever format they like -- "Build
    successful: x.prg", "BUILD SUCCESSFUL -> x.prg", "Wrote x.prg" -- so any
    token ending in .prg is a candidate. That is loose on purpose, which is
    exactly why the candidate must also be a file written since this build
    started: scripts routinely mention .prg paths they did NOT just produce
    (an `rm -f bin/App-test.prg` line, a warning quoting last week's binary, a
    usage string). Taking the first one that merely *exists* is how a stale or
    unrelated build ends up in a screenshot -- the same failure the directory
    scan's mtime floor exists to prevent, so it applies here too.

    Ties go to the newest, since a script that names several is most likely
    naming its final output last.
    """
    fresh = []
    for m in re.finditer(r"(\S+\.prg)", output):
        cand = m.group(1).strip("\"'()[],")
        if sys.platform == "win32" and cand.startswith("/"):
            # A script run under Git Bash names its output the MSYS way,
            # /c/... or /tmp/..., which Windows cannot open as such.
            from ..winbash import to_windows_path
            cand = to_windows_path(cand)
        full = cand if os.path.isabs(cand) else os.path.join(app_dir, cand)
        try:
            if os.path.isfile(full) and os.path.getmtime(full) >= since:
                fresh.append(full)
        except OSError:
            continue
    return max(fresh, key=os.path.getmtime) if fresh else None


def _prg_written_since(app_dir: str, since: float) -> str | None:
    """Newest .prg under the usual output dirs that was written after `since`."""
    found = []
    for rel in BUILD_OUTPUT_DIRS:
        d = os.path.join(app_dir, rel)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.endswith(".prg"):
                continue
            path = os.path.join(d, name)
            try:
                if os.path.getmtime(path) >= since:
                    found.append(path)
            except OSError:
                continue
    return max(found, key=os.path.getmtime) if found else None


# Upper bound on a nav step's own "retries". Steps come from the wire (MCP,
# ciqkit.toml): each retry costs a full settle window, and the REACH chunk's
# max_s is budgeted for the default of 2, so a large value would only turn a
# missed gesture into a chunk timeout.
MAX_STEP_RETRIES = 3


def _step_retries(step: dict) -> int:
    try:
        n = int(step.get("retries", 2))
    except (TypeError, ValueError):
        return 2
    return max(0, min(n, MAX_STEP_RETRIES))


# Command run by :meth:`SimSession.build` inside a project directory when the
# project has a scripts/build.sh. CIQKIT_BUILD_CMD overrides it (`{device}` is
# substituted); with neither, ciqkit.build runs monkeyc itself.
DEFAULT_BUILD_CMD = _build.PROJECT_BUILD_CMD

# What the default command means on Windows, tried in order: the same script
# under Git Bash, then a native script beside it. See _build_argv.
_WIN_NATIVE_SCRIPTS = ("build.cmd", "build.bat", "build.ps1")


def _build_argv(cmd: str, app_dir: str) -> list[str]:
    """Turn the build command into an argv this platform can execute.

    POSIX: shlex, as before.

    Windows needs three things POSIX does not:

      * a ``.sh`` script cannot be executed directly -- it is handed to Git for
        Windows' bash, never to the ``bash.exe`` on PATH, which on a stock
        Windows 11 box is the WSL launcher and would run the build inside a
        Linux VM that cannot see the Windows SDK (see ciqkit.winbash);
      * with the DEFAULT command and no Git Bash, a native
        ``scripts\\build.cmd`` / ``.bat`` / ``.ps1`` is used instead;
      * shlex is not used to split: it eats the backslashes of every Windows
        path in a CIQKIT_BUILD_CMD (``C:\\sdk\\bin\\monkeyc.bat`` became
        ``C:sdkbinmonkeyc.bat``). ``posix=False`` keeps them and quotes are
        stripped by hand.
    """
    if sys.platform != "win32":
        return shlex.split(cmd)
    argv = [a[1:-1] if len(a) >= 2 and a[0] == a[-1] and a[0] in "\"'" else a
            for a in shlex.split(cmd, posix=False)]
    if not argv:
        return argv
    script = argv[0]
    if script.lower().endswith(".sh"):
        from ..winbash import git_bash
        bash = git_bash()
        if bash:
            return [bash, script.replace("\\", "/"), *argv[1:]]
        scripts = os.path.normpath(
            os.path.join(app_dir, os.path.dirname(script.replace("/", os.sep))))
        for native in _WIN_NATIVE_SCRIPTS:
            cand = os.path.join(scripts, native)
            if os.path.isfile(cand):
                if native.endswith(".ps1"):
                    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", cand, *argv[1:]]
                return [cand, *argv[1:]]
        raise SimError(
            f"{script} is a shell script, and there is no Git for Windows bash "
            f"to run it with (the bash.exe on PATH, if any, is WSL's and cannot "
            f"see this SDK). Install Git for Windows, set CIQKIT_BASH to its "
            f"bash.exe, add a scripts\\build.cmd, or set CIQKIT_BUILD_CMD.")
    found = shutil.which(script, path=os.pathsep.join([app_dir, os.environ.get("PATH", "")]))
    if found and not os.path.isabs(script) and not script.startswith("."):
        argv[0] = found
    elif script.startswith(".") and os.path.isfile(os.path.join(app_dir, script)):
        # CreateProcess resolves a relative program against OUR working
        # directory, not the child's cwd, so `.\scripts\build.cmd` has to be
        # made absolute against the project.
        argv[0] = os.path.normpath(os.path.join(app_dir, script))
    return argv


def app_identity(app: str, project: str | None = None,
                 app_dir: str | None = None, prg: str | None = None) -> str:
    """Stable key for "which app is currently loaded in the simulator".

    This is what the warm-load shortcut and the broker's reload check compare,
    so it must identify the CHECKOUT, not just the app name. It used to be the
    project directory's basename, which two git worktrees of the same repo
    share -- the normal layout when several agents work in parallel. Under that
    key, job B's warm-skip would decline to push and hand back job A's screen
    as a fully verified screenshot of B's build.

    The key is "<resolved project path>::<app name>": the path separates two
    checkouts that share a basename, and the name separates two apps built out
    of the same directory. Resolved via realpath so a symlinked checkout is not
    mistaken for a second one; the path half is empty when nothing but a name
    is available.
    """
    root = project or app_dir
    if not root and prg:
        # A .prg normally lives at <project>/<bin|build|gen>/<name>.prg.
        root = os.path.dirname(os.path.dirname(os.path.abspath(prg)))
    return f"{os.path.realpath(root) if root else ''}::{app}"


class SimSession:
    def __init__(self, sdk: str | None = None, out_dir: str | None = None):
        self.sdk = sdk or detect_sdk()
        self.out_dir = out_dir or default_out_dir()
        os.makedirs(self.out_dir, exist_ok=True)
        self._hwnd = None
        self._canvas = None
        # Warm-app tracking: (app_id, device) currently loaded, or None.
        self.current = None
        self._seed_backup = None   # (path, original_bytes) while a seed is active
        self._warned_devices = set()   # unknown device ids already reported
        # Sim app storage journal for the app last pushed (see storage.py):
        # snapshot before every cold push, pinned next to the shots on a crash.
        self._storage_run: storage.StorageRun | None = None
        # The pushed app's console (monkeydo's stdout) and how far a marker wait
        # has read into it. Reset by every push.
        self._console_path = os.path.join(self.out_dir, "console.log")
        self._console_pos = 0

    # ── launch ────────────────────────────────────────────────────────────────
    def ensure(self, project: str = ".") -> dict:
        """Sim is up + paintable. Runs the permission/SDK preflight once."""
        preflight.check_once()
        su.init(self.sdk, project)
        # ensure_simulator() raises SimulatorLaunchError; older builds of the
        # capture layer used sys.exit(1) instead. Trap BOTH and convert to a
        # SimError so the broker fails the job cleanly (and runs its restore)
        # rather than losing the thread to a stray SystemExit.
        try:
            self._hwnd = su.ensure_simulator()
        except su.SimulatorLaunchError as exc:
            raise SimError(str(exc)) from exc
        except SystemExit as exc:
            raise SimError(
                "simulator did not start (timeout). On a remote or SSH session "
                "it aborts with no local WindowServer -- run on the console."
            ) from exc
        if not self._hwnd:
            raise SimError("simulator did not start (see preflight / DiagnosticReports)")
        self._canvas = su.find_canvas(self._hwnd)
        return {"ok": True, "hwnd": str(self._hwnd)}

    def _rebind(self):
        self._hwnd = su.find_sim_window()
        if not self._hwnd:
            self.ensure()
        self._canvas = su.find_canvas(self._hwnd)

    def _capture(self, *, grab_settle_s: float | None = None,
                 refocus: bool = True):
        # Named grab_settle_s, not settle_s, because record() already has a
        # settle_s of its own (the one-time pre-clip settle) live in the same
        # scope as the loop that calls this.
        if not self._hwnd:
            self._rebind()
        img, _scale = su.capture_window(self._hwnd, grab_settle_s=grab_settle_s,
                                        refocus=refocus)
        return img

    # ── build (minimal; caller may pre-build instead) ──────────────────────────
    def build(self, app_dir: str, device: str = "venu3",
              timeout_s: float = 300.0, build_cmd: str | None = None,
              tests: bool = False) -> str:
        """Run the project's own build command and return the built .prg path.

        This EXECUTES a script from the project directory (by default
        ``./scripts/build.sh -d <device>``, or the built-in ``monkeyc`` build
        when there is none), so only point it at projects you
        trust -- the same trust level as running ``make`` in a checkout.
        Callers that already have a .prg should pass it instead and never reach
        this method.

        Locating the built .prg is deliberately strict. Build scripts announce
        their output in whatever format they like, so any token ending in
        ``.prg`` in the build's own output counts as a candidate; if none
        survives we fall back to scanning the usual output directories. Both
        routes accept only a file written AFTER this build started.

        That mtime floor is the whole point: without it a project whose build
        output we could not parse -- or one that merely *mentions* another .prg
        -- silently yields a stale artefact, which may be a unit-test build or
        last week's binary. It loads, the screen changes, verification passes,
        and the screenshot is of something you never built.
        """
        app_dir = os.path.abspath(app_dir)
        if not os.path.isdir(app_dir):
            raise SimError(f"project directory not found: {app_dir}")
        try:
            builtin, template = _build.resolve_build(app_dir, device,
                                                     build_cmd=build_cmd,
                                                     tests=tests)
        except (_build.BuildError, ValueError) as exc:   # ValueError: ConfigError
            raise SimError(str(exc)) from exc
        if tests and builtin is None and template != _build.toml_test_cmd(app_dir):
            # A user build command has no placeholder for "with -t", so running
            # it would hand back a NORMAL build -- which contains no test cases
            # and would "pass" with zero tests. Refuse rather than guess.
            raise SimError(
                f"cannot build the unit-test variant: this project uses a custom "
                f"build command ({template}), which has no test variant. Pass "
                f"--prg with a build made by `monkeyc -t`, set `[build] test_cmd` "
                f"in ciqkit.toml, or remove the custom build command to use the "
                f"built-in test build.")
        if builtin is not None:
            argv = builtin
            cmd = " ".join(shlex.quote(a) for a in argv)
        else:
            cmd = template.format(device=device)
            script = (shlex.split(cmd) if sys.platform != "win32"
                      else shlex.split(cmd, posix=False) or [""])[0]
            if script.startswith("./") and not os.path.isfile(
                    os.path.join(app_dir, script[2:])):
                raise SimError(
                    f"no build script at {os.path.join(app_dir, script[2:])}. "
                    f"Pass an already-built --prg, or fix CIQKIT_BUILD_CMD.")
            argv = _build_argv(cmd, app_dir)
        # Floor for the freshness check below. Taken before the build starts and
        # nudged back a second, because filesystem mtimes are coarser than
        # time.time() on some filesystems and a fast build can land "before" it.
        started = time.time() - 1.0
        try:
            # UTF-8 with replacement, not the locale: monkeyc prints UTF-8,
            # and decoding it as cp1252 raised UnicodeDecodeError on the first
            # non-ASCII byte -- a successful build reported as a crash.
            r = subprocess.run(argv, cwd=app_dir, capture_output=True,
                               encoding="utf-8", errors="replace",
                               stdin=subprocess.DEVNULL, timeout=timeout_s,
                               **hidden_console_kwargs())
        except FileNotFoundError as exc:
            raise SimError(f"build command not executable: {cmd} ({exc})") from exc
        except subprocess.TimeoutExpired as exc:
            raise SimError(f"build timed out after {timeout_s:.0f}s: {cmd}") from exc
        if r.returncode != 0:
            raise SimError(f"build failed: {r.stderr.strip()[-400:] or r.stdout.strip()[-400:]}")

        announced = _announced_prg(r.stdout + "\n" + (r.stderr or ""), app_dir,
                                   started)
        if announced:
            return announced

        fresh = _prg_written_since(app_dir, started)
        if fresh:
            return fresh
        raise SimError(
            f"the build command reported success but produced no .prg newer "
            f"than the moment it started, and its output did not name a fresh "
            f"one.\n"
            f"  command: {cmd}\n"
            f"  looked in: {', '.join(BUILD_OUTPUT_DIRS)} under {app_dir}\n"
            f"Refusing to fall back to an existing .prg -- that is how a stale "
            f"or unrelated build (a -t test binary, say) ends up in a "
            f"screenshot. Pass --prg explicitly if your build writes elsewhere.")

    # ── load + verify ──────────────────────────────────────────────────────────
    def load(self, app: str, prg: str, device: str = "venu3",
             project: str | None = None, force: bool = False,
             max_attempts: int = 3, anchor=None,
             storage_start: str = "fresh", fit: str | None = None) -> dict:
        """Push the .prg and VERIFY it loaded -- not the watchface, not blank,
        not Connect IQ's loading screen.

        The check is deliberately brightness-independent. An earlier "does this
        look like app content" heuristic false-rejected both bright screens (a
        white QR page) and dark ones (a menu), and the restart it triggered on
        every miss churned the simulator into real wedges. "Loaded" here means
        the round face is not blank, is not the loading screen, and either the
        loading screen was seen to come and go since the push, or the face
        changed from the pre-push frame.

        Recovery escalates: re-push, then kill and relaunch the simulator.

        Every cold push first journals the app's simulator storage and then
        starts it from ``storage_start``: ``"fresh"`` (default -- empty
        storage, never another run's leftovers), ``"keep"`` (inherit, still
        journaled) or a fixture directory. A warm hit touches nothing.

        ``fit`` (a .fit/.gpx path) starts the simulator's FIT playback once
        the app is verified loaded -- see :meth:`fit_playback`. A playback that
        did not start fails the load: the app is up but not fed the data the
        caller asked for, and a screenshot of that would be confidently wrong.
        """
        res = self._load(app, prg, device, project, force, max_attempts, anchor,
                         storage_start)
        if fit:
            res["fit"] = self.fit_playback(fit)
        return res

    def _load(self, app, prg, device, project, force, max_attempts, anchor,
              storage_start) -> dict:
        key = app_identity(app, project=project, prg=prg)
        if not force and self.current == (key, device):
            try:
                # "Is the app still on screen?" -- retried by the caller on
                # failure, so it does not need the 1 s settle. Not blank AND
                # not Connect IQ's loader: an app that exited (BACK on its
                # main view does that) leaves the loader triangle up, which is
                # not blank, and the warm path used to call that "verified"
                # and aim the next gestures at an app that was not running.
                img = self._capture(grab_settle_s=0.0)
                if not self._is_blank(img) and not self._is_loading_screen(img):
                    return {"ok": True, "warm": True, "app": app,
                            "verified": "warm"}
            except Exception:
                pass
        su.init(self.sdk, project or os.path.dirname(os.path.dirname(prg)))
        self.ensure(project or ".")
        self._loading_device = device
        try:
            return self._cold_load(app, prg, device, key, max_attempts, anchor,
                                   storage_start)
        finally:
            self._loading_device = None

    def _cold_load(self, app, prg, device, key, max_attempts, anchor,
                   storage_start) -> dict:
        self._storage_begin(prg, app, "load", storage_start)
        before_sig = self._safe_sig()
        relearned = False
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            if attempt > 1 and self._storage_run is not None:
                # a push that did not verify still booted the app, which may
                # have written storage: every attempt starts from the declared
                # state, not from what the previous attempt left
                _safe(self._storage_run.reapply)
            pushed_at = time.time()
            token_before = _safe(su.device_token)
            probe_s = self._push(device, prg)
            ok, confirmed, how = self._verify_loaded(before_sig,
                                                     timeout=42.0 + probe_s,
                                                     anchor=anchor,
                                                     token_before=token_before)
            if ok and not relearned and self._storage_run is not None \
                    and _safe(self._storage_run.after_push, pushed_at) is False:
                # Same app UUID already installed under another name: the
                # push booted on THAT storage. The start state has now been
                # applied to it; push once more so this run really starts
                # from it. Not counted as a failed attempt.
                # before_sig stays the pre-push baseline: the app's own screen
                # would make the re-push look like "nothing changed".
                relearned = True
                attempt -= 1
                continue
            if ok:
                self.current = (key, device)
                return {"ok": True, "warm": False, "app": app,
                        "attempts": attempt, "anchor_confirmed": confirmed,
                        "verified": how,
                        "storage": (self._storage_run.entry or {}).get("storage_name")
                        if self._storage_run else None}
            if attempt < max_attempts:
                # Escalate: a wedged sim won't recover from another monkeydo alone.
                self.restart()
                before_sig = self._safe_sig()
        self.current = None
        if before_sig is None:
            raise SimError(
                f"app {app} did not load after {max_attempts} attempts, and the "
                f"pre-push baseline frame could not be captured either, so the "
                f"'the screen changed' proof was unavailable. Refusing to call "
                f"the load verified -- the device watchface would satisfy every "
                f"remaining check. Check that the simulator window is visible "
                f"and unobscured (`ciqkit doctor`), or pass an --anchor.")
        raise SimError(
            f"app {app} did not load after {max_attempts} attempts: the screen "
            f"never became app content (it stayed blank, stayed on Connect IQ's "
            f"loading screen, or never changed from the watchface)")

    def _storage_begin(self, prg: str, app: str | None, kind: str, start: str):
        """Journal + reset ``prg``'s sim storage. A failure to journal never
        fails the load; a declared fixture that cannot be applied does."""
        self._storage_run = storage.StorageRun(prg, kind=kind, app=app, start=start,
                                               quiet=True).begin()

    def _storage_crash_check(self, artifacts: str | None,
                             crash_screen: bool = False) -> str | None:
        """Pin the pre-run storage + CIQ_LOG into ``artifacts`` if the app
        crashed since its push. Returns the pin dir, or None."""
        run = self._storage_run
        if run is None or run.entry is None:
            return None
        if run.result is None:
            text = storage.new_log_text(run.entry.get("ciq_log"))
            if not (crash_screen or storage.crash_entries(text, run.entry["storage_name"])):
                return None
            run.artifacts = artifacts
            if crash_screen:
                run.saw_crash_screen()
            run.finish()
        return (run.result or {}).get("pinned")

    def is_loaded(self, key: str, device: str) -> bool:
        """Is exactly this checkout+device already up? See :func:`app_identity`."""
        return self.current == (key, device)

    def _push(self, device: str, prg: str):
        """Load the .prg via monkeydo (own the orphan-cleanup + modal-dismiss that
        push_to_sim does, without its strict wait_for_app_pixels wait)."""
        try:
            su._kill_orphan_monkeydo()
            su.dismiss_modal_dialogs(verbose=False)
        except Exception:
            pass
        # monkeydo's output is discarded, so its "Unable to connect to
        # simulator." never surfaces -- a push into a simulator that has shown
        # its window but not bound its port yet (every restart) just reads as
        # "did not load" 42 s later and escalates to another restart. Wait for
        # the port first, and give the verify the seconds monkeydo will spend
        # probing a foreign listener (LM Studio on 1234), unless the
        # probe-skipping shim takes that cost away -- see simport.py.
        st = simport.wait_for_port(20.0)
        if not st.reachable:
            log.warning("monkeydo will not reach the simulator: %s",
                        simport.describe(st))
        cmd, env, how = simport.monkeydo_command(self.sdk, prg, device, st=st)
        if how != "shim" and st.foreign_before:
            log.info("%s", simport.describe(st))
        # monkeydo's output used to be discarded. It carries the app's console
        # (System.println) for as long as the app runs, so it goes to a file a
        # `shot_on` marker wait can read -- and a push starts a fresh one.
        self._console_pos = 0
        try:
            out = open(self._console_path, "wb")
        except OSError:
            out = None
        try:
            subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL,
                             stdout=out or subprocess.DEVNULL,
                             stderr=subprocess.STDOUT if out else subprocess.DEVNULL,
                             **hidden_console_kwargs())
        finally:
            if out:
                out.close()
        return 0 if how == "shim" else st.probe_penalty_s

    def _console_bytes(self) -> bytes:
        try:
            with open(self._console_path, "rb") as fh:
                return fh.read()
        except OSError:
            return b""

    def wait_marker(self, marker: str, timeout_s: float = 20.0,
                    regex: bool = False) -> dict:
        """Wait for the app's console to print ``marker`` (a substring, or a
        regex), reading on from where the last wait ended.

        The cursor starts at the push, so a marker printed BEFORE this call --
        while an earlier chunk was still running -- is found at once, and two
        waits for the same text find two prints, in order. Raises SimError with
        the console's last lines when it never appears.
        """
        t0 = time.monotonic()
        while True:
            data = self._console_bytes()
            hit = console.find_marker(data, self._console_pos, marker, regex)
            if hit:
                line, self._console_pos = hit
                return {"ok": True, "marker": marker, "line": line,
                        "waited_s": round(time.monotonic() - t0, 2)}
            if time.monotonic() - t0 >= timeout_s:
                raise SimError(
                    f"marker {marker!r} did not appear on the app's console within "
                    f"{timeout_s:.0f}s. Last lines:\n{console.tail(data)}\n"
                    f"(markers are read from the app pushed last; a warm load "
                    f"pushes nothing, so pass force)")
            time.sleep(0.1)

    def fit_playback(self, path: str) -> dict:
        """Load ``path`` (.fit/.gpx) into Simulation > Activity Data > FIT/GPX
        Playable File and press play; verified by the window showing that file
        and its clock advancing. Raises SimError otherwise (macOS only)."""
        r = su.start_fit_playback(path)
        if not r.get("ok"):
            raise SimError(f"FIT playback did not start: {r.get('reason')}")
        return r

    def _verify_loaded(self, before_sig, timeout: float, anchor=None,
                       token_before=None):
        """Poll until the app is loaded. Three layered signals, strongest first:

          1. frame-diff GATE (cheap): the round face changed from the pre-push
             frame (app replaced the watchface), isn't blank, and isn't Connect
             IQ's own loading screen. Brightness-independent.
          2. OCR CONFIRM (when an anchor is given, ~150ms via Apple Vision): the
             expected screen text is present -> we know it's the RIGHT screen, not
             just A screen (a watchface never shows the app's own labels). A hit
             returns immediately + certain.

        If an anchor is given but a flaky watch font never OCRs it, we still accept
        once the frame is stable (confirmed=False) rather than fail — OCR
        strengthens confidence, it doesn't gate the load. Returns
        (ok, confirmed, how) where confirmed is True (anchor matched) / False
        (anchor given, unmatched) / None (no anchor), and `how` names the
        evidence that actually carried the decision -- "anchor", "loader-seen",
        "push-seen" (Windows: the device panel was re-created since the push)
        or "frame-changed" -- so a caller can tell a proven load from a merely
        plausible one."""
        started = time.time()
        deadline = started + timeout
        hard_deadline = started + timeout * 2   # cap on loading-screen patience
        good_at = None
        saw_loader = False
        # Windows: the device panel is re-created by every push (see
        # su.device_token), which proves the push landed as surely as seeing
        # the loader does -- and is what lets an unchanged rebuild verify
        # without the frame diff, instead of timing out into a restart.
        push_seen = False
        stable_need = 2.5 if anchor else 0.6   # give OCR a few polls to catch up
        while time.time() < deadline:
            try:
                su.dismiss_modal_dialogs(verbose=False)
            except Exception:
                pass
            if token_before is not None and not push_seen:
                tok = _safe(su.device_token)
                push_seen = bool(tok) and tok != token_before
            img = self._poll_capture()
            if img is not None and self._is_loading_screen(img):
                # The app IS on its way -- Connect IQ only shows this while
                # loading. Restarting the simulator now would throw away a load
                # that is about to finish, so wait longer instead, up to a hard
                # cap so a genuinely stuck load still fails.
                saw_loader = True
                deadline = min(hard_deadline, time.time() + 15.0)
                good_at = None
                time.sleep(0.5)
                continue
            if img is not None and self._is_app_frame(img, before_sig,
                                                      saw_loader or push_seen):
                if anchor and ocr.contains_any(img, anchor) is True:
                    return True, True, "anchor"           # certain: right screen
                if good_at is None:
                    good_at = time.time()
                elif time.time() - good_at >= stable_need:
                    how = ("loader-seen" if saw_loader else
                           "push-seen" if push_seen else "frame-changed")
                    return True, (False if anchor else None), how
            else:
                good_at = None
            time.sleep(0.5)
        return False, False, "none"

    def _is_app_frame(self, img, before_sig, saw_loader: bool = False) -> bool:
        if self._is_blank(img):
            return False
        if self._is_loading_screen(img):
            # Connect IQ's own pre-load screen. It satisfies both the
            # not-blank and the changed-from-the-watchface tests, so without
            # this the load "succeeds" and the screenshot is of the loader --
            # exactly the confidently-wrong output this class is meant to
            # prevent. Keep polling instead.
            return False
        if saw_loader:
            # We watched the loading screen come and go since the push, so this
            # frame is the app that just finished loading. Accept it without a
            # frame diff: re-running the same build shows a screen IDENTICAL to
            # the one already up, and requiring a difference would time out and
            # trigger a pointless simulator restart every time.
            return True
        if before_sig is not None:
            # App loaded => the round face changed from the pre-push frame.
            return su.signatures_differ(before_sig, self._face_sig(img))
        # No baseline. This used to `return True` -- "any non-blank frame
        # counts" -- on the theory that it only happens on a cold start. It
        # does not: `_safe_sig` swallows every capture error, so a single
        # transient grab failure at push time landed here too, and then a
        # silently failed push left the DEVICE WATCHFACE on screen. Not blank,
        # not the loader, no baseline to differ from -> "verified". The one
        # thing this class exists to prevent.
        #
        # So: no baseline means the frame-diff leg of the proof is missing, and
        # this frame cannot carry the load on its own. Keep polling; if the
        # loader shows up, or an anchor matches, those are real evidence. If
        # neither ever does, the load fails and says why.
        log.debug("no pre-push baseline: refusing to accept a frame on "
                  "not-blank alone")
        return False

    def _is_loading_screen(self, img) -> bool:
        """Is this Connect IQ's pre-load screen (a blue triangle on black)?

        Keyed on a signature that real app screens do not have: an almost
        entirely black face carrying a small strongly blue-dominant mark and
        essentially nothing else. A real dark screen has text or chrome, which
        lands in "other" and pushes it past the threshold, so this does not
        false-reject a legitimately minimal screen.
        """
        try:
            import numpy as np
        except ImportError:
            return False
        try:
            face = self._crop_face(img) or img
            a = np.asarray(face.convert("RGB"), dtype=np.int16)
            if a.size == 0:
                return False
            r, g, b = a[..., 0], a[..., 1], a[..., 2]
            luma = 0.299 * r + 0.587 * g + 0.114 * b

            dark = luma < 24
            blue = (b > r + 40) & (b > g + 40) & (b > 60)
            other = ~dark & ~blue

            # Only the round display counts. The crop is its bounding SQUARE,
            # and where the simulator draws the device (always, on Windows)
            # the square's corners are grey bezel and tick marks -- ~21 % of
            # the area, all "other", which hid the loader from this check.
            h, w = dark.shape
            yy, xx = np.ogrid[:h, :w]
            inside = ((xx - (w - 1) / 2.0) ** 2 / max(1.0, (w / 2.0) ** 2)
                      + (yy - (h - 1) / 2.0) ** 2 / max(1.0, (h / 2.0) ** 2)) <= 0.95
            total = float(inside.sum()) or 1.0
            dark_f = (dark & inside).sum() / total
            blue_f = (blue & inside).sum() / total
            other_f = (other & inside).sum() / total
            return bool(dark_f >= 0.85 and 0.003 <= blue_f <= 0.12
                        and other_f <= 0.03)
        except Exception:
            return False

    def _is_blank(self, img) -> bool:
        """Near-uniform frame = blank / launcher / solid watchface load screen."""
        try:
            from PIL import ImageStat
            st = ImageStat.Stat(img.convert("L"))
            return (st.stddev[0] if st.stddev else 0.0) < 6.0
        except Exception:
            return False

    def _safe_capture(self, grab_settle_s: float | None = None):
        try:
            self._rebind()
            return self._capture(grab_settle_s=grab_settle_s)
        except Exception:
            return None

    def _poll_capture(self):
        """A capture for a RETRY LOOP: raised, but with no settle.

        `capture_window`'s 1 s settle exists so a single one-shot screenshot
        cannot catch a window still coming forward. A polling loop already has
        that property for free -- it is going to look again in half a second
        either way, and a torn frame just costs it one more iteration -- so the
        settle is a second of dead time multiplied by every poll. `_verify_loaded`
        polls for up to 42 s: at ~3.0 s an iteration it managed ~14 looks, at
        ~2.0 s it gets ~21, and each look is a chance to catch the loading
        screen that proves the push landed. The raise is kept: mid-load the
        simulator throws modals around, so being sure we photograph the right
        window still earns its ~0.85 s.
        """
        return self._safe_capture(grab_settle_s=0.0)

    def _safe_sig(self, attempts: int = 3):
        """Face signature of the current frame, or None if we truly can't get one.

        Retried, unlike a plain capture: this is the BASELINE for the load's
        "the screen changed" proof, and losing it to one transient grab failure
        used to downgrade that proof to nothing at all.
        """
        for i in range(attempts):
            img = self._poll_capture()
            if img is not None:
                try:
                    return self._face_sig(img)
                except Exception:
                    pass
            if i + 1 < attempts:
                time.sleep(0.3)
        log.warning("could not capture a pre-push baseline frame after %d "
                    "attempts; this load can only be verified by the loading "
                    "screen or an OCR anchor", attempts)
        return None

    # ── verified input ─────────────────────────────────────────────────────────
    def _face_circle(self, img):
        # find_watch_face_auto returns (cx, cy, radius) -- exactly what the
        # signature functions want. Pass the device resolution so it snaps the
        # radius correctly; fall back to a centred guess.
        res = self._device_res()
        # The declared display rect first: it does not move with the screen's
        # content, so before/after signatures compare the same pixels.
        r = self._sim_display_circle(img)
        if r is None:
            try:
                r = su.find_watch_face_auto(img, device_res=res)
            except Exception:
                r = None
        if r and len(r) == 3:
            return r
        w, h = img.size
        return w // 2, h // 2, min(w, h) // 4

    def _face_sig(self, img):
        return su.face_region_signature(img, *self._face_circle(img))

    def _frame_detail(self, img):
        return su.frame_detail_signature(img)

    def _input_changed(self, before, after) -> bool:
        """Did a gesture change the screen? The coarse whole-face test, OR a
        strong local change (see ``details_differ``): placing one mark on a dark
        game board moves the 4x4 average by a few levels, far under the coarse
        threshold, and the retry that followed was a second, real move."""
        if su.signatures_differ(self._face_sig(before), self._face_sig(after)):
            return True
        return su.details_differ(self._frame_detail(before), self._frame_detail(after))

    # How long act() keeps looking for a gesture's effect before it decides the
    # gesture did not land. The simulator can take well over a second to
    # redraw after an input (a busy app, a cold JIT, a slow machine), and
    # judging "no change" too early is what turns a retry into a second input.
    SETTLE_S = 2.5
    SETTLE_POLL_S = 0.4
    # How long act() waits, before the first input, for two consecutive frames
    # to agree. Right after a cold load the app may still be drawing (and the
    # simulator not yet taking input); a gesture sent then is dropped, and a
    # baseline frame taken mid-draw makes the next frame look "changed".
    STABLE_S = 2.0

    def _wait_stable(self):
        """A baseline frame the screen has settled on (or the last one taken
        when ``STABLE_S`` runs out)."""
        frame = self._capture()
        deadline = time.monotonic() + self.STABLE_S
        while time.monotonic() < deadline:
            time.sleep(self.SETTLE_POLL_S)
            nxt = self._capture()
            if not self._input_changed(frame, nxt):
                return nxt
            frame = nxt
        return frame

    @_with_dismissed
    def act(self, kind: str, x: float = 0.5, y: float = 0.5,
            direction: str = "up", key: str = "enter", delay: float = 1.5,
            confirm: bool = True, anchor=None, retries: int = 2,
            settle_s: float | None = None, button: str | None = None,
            hold: bool | None = None, state: str | None = None,
            swipe: dict | None = None, path: str | None = None) -> dict:
        """One verified gesture. kind: tap|hold|swipe|key|button, or the ops
        ``close_aux`` (close the simulator's auxiliary windows), ``phone``
        (``state``: connected|disconnected -- the simulated phone link) and
        ``fit`` (``path``: start FIT playback of that file).

        ``swipe`` ({from: (x, y), direction, dist}, see ``protocol.swipe_spec``)
        gives a swipe an explicit start point and length in display fractions;
        without it a swipe is the default centred flick.

        ``button`` (kind "button") names a physical key from the device's
        simulator.json -- enter, esc, up, down, menu, ... -- and clicks its
        spot on the device art; ``hold`` (default: the key's own ``isHold``)
        presses it for ~1.6 s.

        When ``confirm`` is true, the screen must change (see
        ``_input_changed``) or an OCR anchor appear. The baseline is a STABLE
        frame (see ``_wait_stable``), and the effect is polled for up to
        ``settle_s`` before the gesture counts as not landed.

        Only then is the input re-sent, up to ``retries`` times. That order is
        what makes a retry safe for non-idempotent input: judging from one
        frame right after the input re-sent START presses that HAD landed (a
        second game move, a restart from a result screen). After a full settle
        window with no effect the input verifiably did not land -- which does
        happen, most often to the first key after a cold load -- so re-sending
        it cannot act twice. Retries are logged.
        """
        if settle_s is None:
            settle_s = self.SETTLE_S
        if kind == "close_aux":
            return self.close_aux()
        if kind == "phone":
            return self.set_phone(state)
        if kind == "fit":
            try:
                return {**self.fit_playback(path), "confirmed": True}
            except SimError as exc:
                return {"ok": False, "confirmed": False, "reason": str(exc)}
        su.dismiss_modal_dialogs(verbose=False)
        if kind == "button":
            self._resolve_button(button)      # unknown id: fail before any input
        last_reason = ""
        before = self._wait_stable() if confirm else None
        # Aim at the display once: it does not move between attempts. An
        # unconfirmed gesture has no baseline, so take one frame for it -- but
        # only where gestures are aimed at the display (Windows).
        if before is None and su.FRACTIONS_OF_DISPLAY:
            self._note_display(self._capture())
        else:
            self._note_display(before)
        for attempt in range(retries + 1):
            if attempt:
                log.info("act %s%s: no effect after %.1fs; re-sending (attempt %d of %d)",
                         kind, f" {button if kind == 'button' else key}"
                         if kind in ("key", "button") else "", settle_s,
                         attempt + 1, retries + 1)
            if kind == "button":
                self._do_input(kind, x, y, direction, button, delay, hold=hold)
            else:
                self._do_input(kind, x, y, direction, key, delay, swipe=swipe)
            if not confirm:
                return {"ok": True, "confirmed": False}
            deadline = time.monotonic() + settle_s
            while True:
                after = self._capture()
                changed = self._input_changed(before, after)
                anchor_ok = ocr.contains_any(after, anchor) if anchor else None
                # `anchor_ok is None` covers BOTH "no anchor asked for" and "OCR
                # is unavailable or read nothing", and both mean the same thing:
                # the anchor is a skipped check, so fall back to the frame diff.
                # Keying this on `anchor is None` instead made a machine without
                # an OCR backend fail every anchored gesture outright -- an
                # anchor gating a result it was only ever meant to confirm.
                if anchor_ok is True or (anchor_ok is None and changed):
                    return {"ok": True, "confirmed": True, "changed": changed,
                            "anchor": anchor_ok, "attempts": attempt + 1}
                if time.monotonic() >= deadline:
                    break
                time.sleep(self.SETTLE_POLL_S)
            last_reason = f"no-change (changed={changed}, anchor={anchor_ok})"
            # The screen did not move, so `after` is still the baseline; keep
            # comparing against the original one.
        return {"ok": False, "confirmed": False, "reason": last_reason}

    def _note_display(self, img) -> None:
        """Tell the capture layer where the watch display is in ``img``, so a
        gesture's (x, y) fractions are fractions of the SCREEN.

        Only where the capture layer asks for it (su.FRACTIONS_OF_DISPLAY,
        i.e. Windows, whose device panel always draws the watch body around
        the display). Without it y=0.85 is on the bezel. A face that cannot
        be found leaves the old whole-content behaviour, never a guess.
        """
        if not su.FRACTIONS_OF_DISPLAY or img is None:
            return
        try:
            su.set_display_box(self._hwnd, self._face_box(img))
        except Exception:
            log.debug("could not locate the display for gesture aiming",
                      exc_info=True)

    #: Keys the macOS simulator does not take from the keyboard: a Return
    #: keystroke (System Events key code 36) never reaches the app as START,
    #: and Esc likewise, though arrows do. They are pressed on the device art.
    BUTTON_ROUTED_KEYS = {"enter": "enter", "return": "enter",
                          "esc": "esc", "escape": "esc"}
    HOLD_S = 1.6

    def _device_id(self):
        cur = getattr(self, "current", None)
        return cur[1] if cur else None

    def close_aux(self) -> dict:
        """Close every simulator window except the device window (and alert
        dialogs, which dismiss_modal_dialogs owns)."""
        su.dismiss_modal_dialogs(verbose=False)   # alerts + aux in one pass
        left = [w.name for w in su._sw.classify(su._mac_sim_windows()).aux] \
            if su.IS_MAC else []
        return {"ok": not left, "confirmed": True, "remaining": left}

    def set_phone(self, state) -> dict:
        """Set Settings > Connection Type > BLE to Connected / Not Connected and
        verify the menu shows it. ``DeviceSettings.phoneConnected`` follows."""
        try:
            r = su.set_phone_connection(state)
        except ValueError as exc:
            raise SimError(str(exc)) from exc
        r["confirmed"] = True
        r["phoneConnected"] = (r["state"] == "Connected") if r.get("state") else None
        if not r["ok"]:
            r["reason"] = (f"menu shows {r.get('state')!r}, wanted "
                           f"{r.get('requested')!r}")
        return r

    def _resolve_button(self, button):
        """The device's key entry for ``button``; SimError listing the
        available ids when there is none."""
        dev = self._device_id()
        keys = su.sim_device_keys(dev)
        if not keys:
            raise SimError(
                f"device {dev!r} lists no physical keys in its simulator.json, "
                f"so a `button` step cannot be aimed; use tap/swipe, or `key`")
        for k in keys:
            if k["id"] == button:
                return k
        raise SimError(f"device {dev!r} has no button {button!r}; available: "
                       f"{', '.join(sorted({k['id'] for k in keys}))}")

    def _press_button(self, button, hold, delay):
        key = self._resolve_button(button)
        img = self._capture()
        found = su.sim_button_point(img, self._device_id(), button,
                                    scale=getattr(self._canvas, "retina_scale", None))
        if found is None:
            raise SimError(f"could not align the capture with {self._device_id()}'s "
                           f"device art to aim at button {button!r} (is the "
                           f"simulator showing the device art, at its normal size?)")
        x, y, _ = found
        held = key["hold"] if hold is None else bool(hold)
        su.pm_press_point(self._canvas, x, y,
                          hold_s=self.HOLD_S if held else 0.0, delay=delay)

    def _do_input(self, kind, x, y, direction, key, delay, hold=None, swipe=None):
        """Aim one gesture at the watch DISPLAY, in display fractions.

        Fractions, via pm_click_frac / pm_swipe_frac, because pixels here meant
        two different things. `x`, `y` were multiplied by the window's LOGICAL
        size, while the offsets the capture layer applied were in captured-image
        (physical) pixels -- and the two were then added. It never showed up
        because `_rebind()` below rebuilds the window handle immediately before
        every gesture, which used to reset those offsets to zero. So the sum was
        always right by accident, and always aimed at a fraction of the WHOLE
        WINDOW rather than of the display: every tap was off by the simulator's
        chrome.
        """
        self._rebind()
        su.restore_window(self._hwnd)
        time.sleep(0.2)
        self._rebind()
        c = self._canvas
        if kind == "tap":
            su.pm_click_frac(c, x, y, delay=delay)
        elif kind == "swipe" and swipe:
            fx, fy = swipe["from"]
            su.pm_swipe_from_frac(c, fx, fy, direction=swipe["direction"],
                                  dist=swipe["dist"], delay=delay)
        elif kind == "swipe":
            # ~84% of the display width: the flick has to be long enough for
            # the sim to classify it as a swipe (a short drag lands as a tap).
            # It is centred on the display, so up/down have room either way.
            su.pm_swipe_frac(c, direction=direction, span=0.84, delay=delay)
        elif kind == "button":
            self._press_button(key, hold, delay)
        elif kind == "key":
            routed = self.BUTTON_ROUTED_KEYS.get(str(key).lower())
            if (su.IS_MAC and routed
                    and any(k["id"] == routed
                            for k in su.sim_device_keys(self._device_id()))):
                self._press_button(routed, False, delay)
            else:
                su.phys_key(self._hwnd, key, delay=delay)
        elif kind == "hold":
            su.pm_hold_frac(c, x, y, delay=delay)
        else:
            raise SimError(f"unknown act kind: {kind}")

    def reach(self, steps: list, anchor=None, screen: str | None = None) -> dict:
        """Run a nav recipe: apply each gesture, then verify the target anchor.

        Lives here, not in the broker, because it is simulator work: it drives
        gestures and reads the screen. The broker's job is to decide WHEN this
        runs and to keep it the only thing running -- not to know what a
        navigation step is. It used to be implemented in the broker, reaching
        into ``session._capture()`` and importing the OCR module there.

        The whole recipe is ONE chunk on purpose: nav is stateful. Preempting
        between gestures would let another job load a different app, and the
        resume path reloads this one -- back at its first screen, with the
        remaining gestures landing somewhere they were never meant to.
        """
        done = []
        for i, step in enumerate(steps or []):
            kind = step.get("kind", "tap")
            if kind == "shot_on":
                r = self._shot_on_step(step)
                done.append({"ix": i, "kind": kind, "ok": bool(r.get("ok")),
                             "reason": r.get("reason"), "path": r.get("path"),
                             "line": r.get("line")})
                if not r.get("ok"):
                    return {"ok": False, "screen": screen, "anchor": None,
                            "steps": done,
                            "error": f"step {i + 1} of {len(steps)} (shot_on): "
                                     f"{r.get('reason')}"}
                continue
            r = self.act(kind, step.get("x", 0.5), step.get("y", 0.5),
                         step.get("direction", "up"), step.get("key", "enter"),
                         button=button_id(step), hold=step.get("hold"),
                         state=step.get("state", step.get("value")),
                         confirm=step.get("confirm", True),
                         anchor=step.get("anchor"),
                         retries=_step_retries(step),
                         swipe=swipe_spec(step) if kind == "swipe" else None,
                         path=step.get("path"))
            # Gesture results used to be discarded, so a nav whose every tap
            # missed still reported that it had arrived. A step that could not
            # be confirmed stops the recipe: every gesture after it would be
            # aimed at a screen we know we are not on.
            done.append({"ix": i, "kind": kind, "ok": bool(r.get("ok")),
                         "reason": r.get("reason")})
            for extra in ("dismissed", "simulator_crashed", "state", "phoneConnected"):
                if r.get(extra) is not None:
                    done[-1][extra] = r[extra]
            if not r.get("ok"):
                return {"ok": False, "screen": screen, "anchor": None,
                        "steps": done,
                        # 1-based, "of N": a 0-based "step 1" read as the
                        # first step and sent a diagnosis down the wrong path.
                        "error": f"step {i + 1} of {len(steps)} ({kind}) did not "
                                 f"change the screen: "
                                 f"{r.get('reason')}. Pass \"confirm\": false on "
                                 f"that step if it is not supposed to."}
        img = self._capture()
        anchor_ok = None
        if anchor:
            # NOT bool(): contains_any returns None when OCR is unavailable or
            # read nothing, and None means "skipped". Collapsing that to False
            # made every anchored nav fail on a machine with no OCR backend.
            anchor_ok = ocr.contains_any(img, anchor)
        return {"ok": anchor_ok is not False, "screen": screen,
                "anchor": anchor_ok, "steps": done}

    def _shot_on_step(self, step: dict) -> dict:
        try:
            r = self.shot(step["name"], crop=step.get("crop", True),
                          frame=step.get("frame", True), anchor=step.get("anchor"),
                          **({"out_dir": step["out_dir"]} if step.get("out_dir") else {}),
                          **({"settle_s": step["settle_s"]} if "settle_s" in step else {}),
                          marker=step["marker"],
                          marker_timeout_s=float(step.get("timeout", 20)),
                          marker_regex=bool(step.get("regex", False)))
        except SimError as exc:
            return {"ok": False, "reason": str(exc)}
        return {"ok": True, "path": r["path"], "line": r.get("marker_line"),
                "anchor": r.get("anchor")}

    # ── screenshot ─────────────────────────────────────────────────────────────
    @_with_dismissed
    def shot(self, name: str, crop: bool = True, frame: bool = True, anchor=None,
             out_dir: str | None = None,
             settle_s: float = DEFAULT_SHOT_SETTLE_S, marker: str | None = None,
             marker_timeout_s: float = 20.0, marker_regex: bool = False) -> dict:
        # Let the app settle before capturing — the launch "notification" banner
        # (an app's first-run / empty-state toast) auto-dismisses a couple of
        # seconds after boot and the real page then renders; shooting too early
        # catches the banner. dismiss_modal_dialogs clears stray sim dialogs too.
        #
        # frame=True (the DEFAULT) composites the captured face onto Garmin's SDK
        # device PNG — the full watch body + silicone band — so store images show
        # the watch WITH its strap, not a bare cropped bezel. Pass frame=False
        # (native) to get the tight round-face crop instead. Framing falls back to
        # the raw crop if face detection or the SDK device PNG is unavailable.
        # `marker`: take the shot when the app's console prints that text (an
        # app's own "page N is up" line) instead of after a guessed sleep.
        # `settle_s` then counts from the marker.
        hit = (self.wait_marker(marker, marker_timeout_s, marker_regex)
               if marker else None)
        su.dismiss_modal_dialogs(verbose=False)
        self._rebind()
        su.restore_window(self._hwnd)
        if settle_s > 0:
            time.sleep(settle_s)
        img = self._capture()
        od = out_dir or self.out_dir
        os.makedirs(od, exist_ok=True)
        path = os.path.join(od, f"{name}.png")
        out_img = img
        framed_ok = False
        if frame:
            f = self._framed(img)
            if f is not None:
                out_img = f
                framed_ok = True
        if not framed_ok and crop:
            out_img = self._crop_face(img) or img
        out_img.save(path)
        # OCR the screen from the tight face crop (framing shrinks + surrounds the
        # screen, which can hurt text recognition).
        anchor_ok = None
        if anchor:
            a_img = self._crop_face(img) or img
            anchor_ok = ocr.contains_any(a_img, anchor)
        res = {"ok": True, "path": path, "anchor": anchor_ok,
               "framed": framed_ok, **({"marker_line": hit["line"]} if hit else {}),
               "loaded": _safe(su._looks_like_app_loaded, img)}
        pinned = _safe(self._storage_crash_check, od)
        if pinned:
            res["crash_evidence"] = pinned
        return res

    # Logical face resolution per device (drives the face detector's scale snap).
    DEVICE_RES = {"venu3": 454, "venu3s": 390, "venu2": 416, "venu2s": 360,
                  "venu2plus": 416, "vivoactive5": 390, "fr965": 454, "fr265": 416,
                  "fr255": 260, "fenix7": 260, "fr955": 260}

    DEFAULT_DEVICE_RES = 454

    def _device_id(self):
        # During a load, the device being loaded -- `current` still names the
        # previous app's device until the load is verified.
        loading = getattr(self, "_loading_device", None)
        if loading:
            return loading
        return self.current[1] if self.current else "venu3"

    def _device_res(self, device=None):
        """Logical face resolution, used to snap the detected face radius.

        The SDK knows this for every device it ships, so ask it first
        (compiler.json -> resolution.width) and keep DEVICE_RES only as an
        offline fallback. The hand-maintained table went stale the moment a
        device outside it was driven: fr265s is a 360px face, the table had no
        entry, and the 454 guess mis-snapped every crop of that run.

        A device neither the SDK nor the table knows still falls back to 454,
        which is wrong for a smaller display and shows up as an oddly cropped
        screenshot rather than an error -- so say so once instead of silently
        guessing.
        """
        dev = device or self._device_id()
        res = self._sdk_device_res(dev) or self.DEVICE_RES.get(dev)
        if res is None:
            if dev not in self._warned_devices:
                self._warned_devices.add(dev)
                log.warning(
                    "unknown device %r: assuming a %dpx face. If crops look "
                    "wrong, add it to SimSession.DEVICE_RES.",
                    dev, self.DEFAULT_DEVICE_RES)
            return self.DEFAULT_DEVICE_RES
        return res

    SDK_DEVICES_DIR = preflight.devices_root()

    def _sdk_device_res(self, dev):
        """`resolution.width` from the installed SDK's device profile, or None
        when the device is not installed / the profile is unreadable. Cached
        per process (a device profile does not change under us) because this
        is read on every capture."""
        if dev in _SDK_RES_CACHE:
            return _SDK_RES_CACHE[dev]
        res = None
        try:
            import json
            path = os.path.join(self.SDK_DEVICES_DIR, dev, "compiler.json")
            with open(path) as fh:
                width = json.load(fh).get("resolution", {}).get("width")
            if isinstance(width, int) and width > 0:
                res = width
        except Exception:
            res = None
        _SDK_RES_CACHE[dev] = res
        return res

    def _face_cxcyrad(self, img):
        """(cx, cy, radius) of the round watch face inside the full sim window, or
        None.

        Device art first: when the simulator drew the watch body itself, the
        glass can be MEASURED against the SDK device PNG's own body-to-screen
        ratio, without reading a screen pixel. The brightness detectors below
        are the fallback, and they are wrong by ~30 px on exactly that capture
        (outside the glass lies the bezel, not bright chrome) -- worse on a
        bright page. Passing the device's logical resolution lets
        find_watch_face_auto snap the radius correctly.
        """
        res = self._device_res()
        try:
            geo = su.device_art_screen_circle(img, self._device_id(), res)
        except Exception:
            geo = None
        if geo:
            return geo
        geo = self._sim_display_circle(img)
        if geo:
            return geo
        for kwargs in ({"device_res": res}, {}):
            try:
                r = su.find_watch_face_auto(img, **kwargs)
            except Exception:
                r = None
            if r and len(r) == 3:
                return r
        return None

    def _sim_display_circle(self, img):
        """(cx, cy, radius) from the device profile's declared display rect
        (su.sim_display_box), or None when the capture is not that device's
        art drawn 1:1. Exact where it applies -- the Windows simulator --
        where the brightness detectors were measured ~110 px off."""
        try:
            box = su.sim_display_box(img, self._device_id())
        except Exception:
            box = None
        if not box:
            return None
        x0, y0, x1, y1 = box
        return (x0 + x1) // 2, (y0 + y1) // 2, min(x1 - x0, y1 - y0) // 2

    def _face_box(self, img):
        r = self._face_cxcyrad(img)
        if not r:
            return None
        cx, cy, rad = r
        W, H = img.size
        box = (max(0, cx - rad), max(0, cy - rad),
               min(W, cx + rad), min(H, cy + rad))
        return box if box[2] > box[0] and box[3] > box[1] else None

    def _crop_face(self, img):
        box = self._face_box(img)
        return img.crop(box) if box else None

    def _framed(self, img):
        """Composite the captured face onto Garmin's SDK device PNG (watch body +
        silicone band) on a premium dark background — the store-ready render. Uses
        the same framer as the batch capture pipeline. Returns None (caller falls
        back to a raw crop) if the face can't be found.

        Implemented as detect-then-apply (:meth:`_frame_geometry` +
        :meth:`_apply_frame`) so :meth:`record` can run the detection once and
        apply the identical transform to every frame of a clip."""
        geom = self._frame_geometry(img)
        if geom is None:
            return None
        try:
            return self._apply_frame(img, geom)
        except Exception:
            return None

    def _frame_geometry(self, img):
        """Detect the store-framing transform for this capture, ONCE.

        Returns a dict describing a transform :meth:`_apply_frame` can replay
        deterministically on any number of frames, or None when no framing is
        possible (no face found). Detection is the content-dependent, per-frame
        unstable part -- the brightness-based face detector has a history of
        returning the chrome instead of the face on bright content -- which is
        exactly why it runs once per clip and never per frame.

        Two modes, probed in order:

          * ``device-art``: the SIMULATOR already drew the watch body + band
            (its device-art display mode) on a white canvas. Re-detecting the
            display and compositing onto a second SDK body double-frames it:
            a ghost ring around shrunken content, worst on bright screens.
            The geometry is the crop box of the sim's own watch.
          * ``sdk``: a bare display capture; the geometry is the face circle
            (cx, cy, rad), the device's native res, and a frozen
            autocrop_screen_box for the face crop.
        """
        try:
            box = su.detect_device_art_geometry(img)
        except Exception:
            box = None
        if box is not None:
            geom = {"mode": "device-art", "box": box, "canvas_mask": None}
            try:
                # The expensive half of framing (~0.45 s of flood fill) reduced
                # to a reusable mask -- see device_art_canvas_mask. Optional:
                # without it every frame just floods again, slowly.
                geom["canvas_mask"] = su.device_art_canvas_mask(img, box)
            except Exception:
                log.debug("device-art canvas mask failed; falling back to a "
                          "per-frame flood fill", exc_info=True)
            return geom
        r = self._face_cxcyrad(img)
        if not r:
            return None
        dev = self._device_id()
        res = self._device_res(dev)
        geom = {"mode": "sdk", "cxcyrad": r, "device": dev, "res": res,
                "autocrop_box": None}
        try:
            cropped = su.crop_face(img, r[0], r[1], r[2], native_res=res)
            geom["autocrop_box"] = su.autocrop_screen_box(cropped)
        except Exception:
            pass
        return geom

    def _apply_frame(self, img, geom):
        """Replay a detected framing transform on one frame.

        Every geometric decision (crop boxes, face circle, native res, the SDK
        composite) comes from ``geom``, so applying the same geometry to every
        frame of a clip yields pixel-stable framing with zero drift."""
        if geom["mode"] == "device-art":
            return su.apply_device_art_frame(img, geom["box"],
                                             canvas_mask=geom.get("canvas_mask"))
        cx, cy, rad = geom["cxcyrad"]
        cropped = su.crop_face(img, cx, cy, rad, native_res=geom["res"])
        if geom.get("autocrop_box"):
            cropped = cropped.crop(geom["autocrop_box"])
        framed = su.make_sdk_framed_screenshot(cropped, geom["device"],
                                               autocrop=False)
        if framed is None:
            # No SDK device PNG — legacy pixel-walk body crop, no real band.
            body = su.crop_watch_body(img, cx, cy, rad)
            framed = su.make_framed_screenshot(body)
        return framed

    # ── animation recording ──────────────────────────────────────────────────
    # SLOWEST median GIF frame duration on playback (ms), ~2 fps. A CEILING,
    # never a target: _compress_durations only ever speeds a track UP to it.
    # Capture runs at ~4.6 fps, comfortably faster, so a healthy clip passes
    # through at its true wall-clock rate and this value never engages. It is
    # the floor of watchability for a DEGRADED clip: when grabs are seconds
    # apart, consecutive frames are separate POSES rather than adjacent motion
    # frames, and each needs long enough on screen to register as a distinct
    # step (a full-scene read takes ~300-500 ms). The old 120 ms default played
    # such poses at pseudo-video rate and the eye registered none of them.
    # Canonical value lives in protocol.py so the CLI/MCP/broker defaults
    # cannot drift from this one.
    DEFAULT_PLAYBACK_MS = _DEFAULT_PLAYBACK_MS

    #: Re-raise the simulator window every Nth frame of a clip. The loop skips
    #: the raise on every other frame -- that is where the old ~0.5 fps went --
    #: so this is the guard for both ways the assumption behind that can break:
    #: something steals the foreground and the rest of the frames silently
    #: capture *it*, or the window MOVES and every later grab reads the wrong
    #: screen region. It is not free: a raise plus its short settle costs
    #: ~1.05 s against a ~0.20 s frame, so at 25 it is still ~20 % of a clip's
    #: wall-clock -- the price of not silently recording the wrong window for
    #: the other 24. Raise it to trade that back. 0 disables it entirely, which
    #: is only safe when nothing else can touch the screen. The stall itself is
    #: kept out of PLAYBACK by _flatten_overhead_gaps.
    DEFAULT_REFOCUS_EVERY = 25

    def record(self, name: str, duration_s: float = 8.0, interval_ms: int = 200,
               crop: bool = True, frame: bool = True,
               out_dir: str | None = None,
               settle_s: float = DEFAULT_SHOT_SETTLE_S, keep_frames: bool = True,
               sheet_cols: int = 6, max_frames: int = 300,
               playback_ms: int = DEFAULT_PLAYBACK_MS,
               refocus_every: int = DEFAULT_REFOCUS_EVERY) -> dict:
        """Sample the running app over time into an animated GIF.

        This is a periodic SAMPLER, not a screen recorder. Every frame is a
        separate window grab, so `interval_ms` is a floor, not a guarantee, and
        a clip steps through states rather than recording true motion.

        Measured on macOS: ~0.22 s/frame, 4.6 fps end to end. It used to be
        ~2.1 s (~0.5 fps), for two reasons that were both ours rather than the
        simulator's. :func:`capture_window` re-probed the window position,
        restored it, focused it and slept a second BEFORE every single grab
        (~1.85 s around a 0.15 s `screencapture`) -- correct for a one-shot
        screenshot, pure overhead in a loop that already raised the window
        once; the loop now raises it only on frame 0 and every `refocus_every`
        frames after. And framing each frame cost ~0.48 s, ~0.47 s of which
        was a flood fill repainting the simulator's white canvas; that region
        is outside the bezel and therefore identical all clip, so it is now
        detected once as a mask and pasted (~0.02 s).

        With ``frame=True`` (the default) every frame is framed exactly like
        :meth:`shot`: the COMPLETE watch (body + band) on the dark store
        background, via the device-art trim or the SDK composite. The framing
        geometry is detected ONCE on the first frame and the identical
        transform is applied to every frame, so the clip cannot jitter --
        re-running the brightness-based detection per frame is known to wobble
        on bright content. Pass ``frame=False`` for the old tight face-box
        crop (also detected once), or ``crop=False`` as well for the raw
        window. Outputs:

          <name>.gif          -- animated GIF, TIME-COMPRESSED for playback:
                                the real capture timestamps set the frame
                                durations' RATIOS (a stall stays visibly longer
                                than a fast frame), then the whole track is
                                scaled so the median frame lasts `playback_ms`.
                                Wall-clock durations would be honest and
                                useless at the old ~2 s/frame: replayed at
                                0.5 fps a clip reads as a still image in every
                                viewer, which is exactly the "the GIF isn't
                                moving" bug. The scale only ever speeds a clip
                                UP, so at the current ~4 fps the track is
                                already faster than `playback_ms` and passes
                                through untouched. Pass playback_ms=0 to force
                                wall-clock timing;
          <name>.sheet.png    — contact sheet (grid of every frame, sheet_cols
                                wide) for frame-by-frame review in tools that
                                can't play a GIF;
          <name>.frames/      — the raw frame PNGs (keep_frames=True).

        The grab is a screen-region capture, so the sim must stay unobscured
        and stationary for the whole clip (the broker being the single sim
        actor makes that the normal case); `refocus_every` re-raises and
        re-locates it periodically in case it does not.

        `max_frames` is a MEMORY bound as much as a runtime one: every frame is
        held as a framed 1080x1080 RGB image until the GIF is written, so the
        300-frame default is ~1 GB resident. That cap used to be unreachable --
        at ~0.5 fps it meant a 10-minute clip -- and at 4.6 fps it is a ~65 s
        one. Raise it deliberately."""
        su.dismiss_modal_dialogs(verbose=False)
        self._rebind()
        su.restore_window(self._hwnd)
        if settle_s > 0:
            time.sleep(settle_s)

        od = out_dir or self.out_dir
        os.makedirs(od, exist_ok=True)

        frames = []   # (PIL.Image transformed, t_captured)
        # Gaps that are OUR bookkeeping, not the app standing still: the one
        # after frame 0 (which also pays for building the canvas mask) and the
        # one before every re-raised frame. See _flatten_overhead_gaps.
        overhead_gaps = set()
        box = None
        geom = None          # framing geometry, detected once on frame 0
        geom_tried = False   # never re-probe mid-clip: mixed framing jitters
        t0 = time.time()
        n = 0
        while (time.time() - t0) < duration_s and n < max_frames:
            target = t0 + (n * interval_ms) / 1000.0
            now = time.time()
            if target > now:
                time.sleep(target - now)
            # Frame 0 pays the full raise + 1 s settle: the framing geometry
            # for the WHOLE clip is detected from it, so it must not catch a
            # half-painted window. A periodic re-raise gets a short settle, not
            # none -- focus_window returns 0.3 s after the AppleScript, and a
            # window that really was buried may not have finished coming
            # forward, so a zero-settle refocus frame can capture the occluder
            # it exists to escape. Amortized over refocus_every it is ~8 ms.
            raising = n == 0 or (refocus_every > 0 and n % refocus_every == 0)
            if n == 0:
                settle = None
            else:
                settle = 0.2 if raising else 0.0
            if raising and n > 0:
                overhead_gaps.add(n - 1)
            img = self._capture(refocus=raising, grab_settle_s=settle)
            t = time.time()
            if frame and not geom_tried:
                geom_tried = True
                overhead_gaps.add(0)   # the mask build lands in gap 0
                geom = self._frame_geometry(img)
                if geom is None:
                    log.warning("record: no framing geometry found on the "
                                "first frame; falling back to the tight face "
                                "crop for this clip")
            elif raising and geom is not None and geom.get("canvas_mask"):
                # Refresh the canvas mask on a re-raised frame -- never the
                # crop BOX, which stays frozen all clip because re-detecting it
                # is what makes a clip wobble. The mask is a different animal:
                # it records which pixels were white canvas at frame 0, and
                # anything dark sitting over the canvas THEN (a notification
                # banner is the realistic one) blocks the flood there and gets
                # baked into every later frame as an un-repainted white patch.
                # Rebuilding it here bounds that to one refocus interval, and
                # also re-tracks the few px of drift the fit check tolerates.
                # This frame already pays ~1 s for the raise, so the ~0.5 s is
                # hidden in a gap the playback flattens away anyway.
                try:
                    geom["canvas_mask"] = su.device_art_canvas_mask(
                        img, geom["box"])
                except Exception:
                    log.debug("record: canvas mask refresh failed on frame %d; "
                              "keeping the previous one", n, exc_info=True)
            if geom is not None:
                try:
                    img = self._apply_frame(img, geom)
                except Exception as exc:
                    # All frames must share one size; a transform that worked
                    # on frame 0 failing later means the window changed under
                    # us -- fail loudly rather than emit a corrupt clip.
                    raise SimError(
                        f"framing failed on frame {n} after succeeding on "
                        f"frame 0 ({exc}); did the simulator window move or "
                        f"resize mid-clip?") from exc
            elif crop:
                if box is None:
                    box = self._face_box(img)
                if box:
                    img = img.crop(box)
            frames.append((img.convert("RGB"), t))
            n += 1
        if not frames:
            raise SimError("record captured no frames")
        truncated = n >= max_frames and (time.time() - t0) < duration_s
        if truncated:
            log.warning("record hit the %d-frame cap after %.1fs of the "
                        "requested %.1fs; the clip is short. The cap bounds "
                        "resident memory (~3.5 MB a frame) as well as runtime",
                        max_frames, time.time() - t0, duration_s)

        gif_path = os.path.join(od, f"{name}.gif")
        # Frame durations from the real timestamps, then time-compressed so the
        # clip actually animates (see the docstring).
        wall = []
        for i in range(len(frames) - 1):
            wall.append(max(20, int((frames[i + 1][1] - frames[i][1]) * 1000)))
        # Flatten BEFORE extending the track, so the final frame cannot inherit
        # a raw overhead gap: overhead_gaps indexes real deltas, and a tail that
        # copied wall[-1] first would keep the un-flattened value if that last
        # delta happened to be a refocus (it is, whenever the clip ends just
        # after one) -- which is exactly how a 1.4 s freeze survived on the
        # final frame of a measured clip.
        wall = _flatten_overhead_gaps(wall, overhead_gaps)
        # The last frame has no successor to time it: reuse the previous real
        # delta rather than the requested interval, which is a floor the grab
        # never reaches and would flash the final frame past in a few ms.
        wall.append(wall[-1] if wall else interval_ms)
        durations, speedup = _compress_durations(wall, playback_ms)
        imgs = [f[0] for f in frames]
        imgs[0].save(gif_path, save_all=True, append_images=imgs[1:],
                     duration=durations, loop=0, optimize=True)

        frames_dir = None
        if keep_frames:
            frames_dir = os.path.join(od, f"{name}.frames")
            os.makedirs(frames_dir, exist_ok=True)
            for i, (im, _t) in enumerate(frames):
                im.save(os.path.join(frames_dir, f"{i:03d}.png"))

        sheet_path = os.path.join(od, f"{name}.sheet.png")
        self._contact_sheet(imgs, sheet_path, cols=sheet_cols)

        elapsed = frames[-1][1] - frames[0][1]
        fps = (len(frames) - 1) / elapsed if elapsed > 0 else 0.0
        return {"ok": True, "gif": gif_path, "sheet": sheet_path,
                "frames_dir": frames_dir, "frames": len(frames),
                "framed": geom is not None,
                "fps_actual": round(fps, 2), "duration_s": round(elapsed, 2),
                "gif_duration_s": round(sum(durations) / 1000.0, 2),
                "playback_speedup": round(speedup, 1),
                "truncated": truncated}

    @staticmethod
    def _contact_sheet(imgs, path: str, cols: int = 6, tile_w: int = 227):
        """Grid montage of every frame (row-major, numbered), for frame-by-frame
        review. Tiles are downscaled to tile_w so a 40-frame clip stays a
        readable, reasonably-sized single PNG."""
        from PIL import Image, ImageDraw
        if not imgs:
            return
        # Never pad out to the full grid width: captures are slow, so a short
        # clip is often 1-2 frames, and a fixed 6-wide grid renders those as one
        # thumbnail beside five empty cells that look like failed captures.
        cols = max(1, min(cols, len(imgs)))
        w, h = imgs[0].size
        scale = tile_w / float(w)
        tw, th = tile_w, max(1, int(h * scale))
        rows = (len(imgs) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * tw, rows * th), (24, 24, 24))
        draw = ImageDraw.Draw(sheet)
        for i, im in enumerate(imgs):
            tile = im.resize((tw, th))
            x, y = (i % cols) * tw, (i // cols) * th
            sheet.paste(tile, (x, y))
            draw.text((x + 4, y + 2), str(i), fill=(255, 235, 59))
        sheet.save(path)

    # ── SEED_* seeding with guaranteed restore + git-clean guard ────────────────
    def seed(self, app_dir: str, consts: dict = None, patches: list = None,
             force: bool = False) -> dict:
        """Prepare a build-time seed. Two kinds, both with a byte-exact backup of
        every file touched so restore() puts them back verbatim (safer than
        regex-reverse) and never leaves SEED_*=true committed:

          consts:  {NAME: value} -> patch `const NAME = VALUE;` in
                   source/Constants.mc (the SEED_* flags).
          patches: [{file, pattern, repl}] -> regex edits on a source file inside
                   app_dir, e.g. disabling an app's GlanceView so the sim opens
                   the main view instead of the glance.

        Two guards, because this writes into your working tree:

          * every target must resolve INSIDE ``app_dir``. A ``file`` of
            ``../../.ssh/config``, or an absolute path, is refused -- so a job
            (or an MCP client that submits one) cannot turn a seed into a write
            primitive for the rest of the filesystem.
          * a target with uncommitted git changes is refused unless ``force``.
            Restore is reliable, but a SIGKILL leaves the patch in place, and
            ``git checkout`` only rescues work that was committed.

        The originals are also written to a crash-safe on-disk journal
        (:mod:`ciqkit.sim.seedjournal`) before anything is modified, so a
        SIGKILLed broker is recoverable rather than merely detectable.

        Only one seed active at a time; every touched file is restored together.
        Edits to the same file COMPOSE, in order: consts first, then each patch
        on the text the previous edit left (two patches on one file used to be
        computed from the original each, so the last one silently dropped the
        others), and the file is backed up once, as it was before any of them.
        """
        if self._seed_backup is not None:
            raise SimError("a seed is already active; restore() first")
        backups = []   # [(abs_path, original_bytes)], one per file, first read
        pending = {}   # abs_path -> text so far; written only once every check passed
        if consts:
            cpath = _seed_target(app_dir, os.path.join("source", "Constants.mc"))
            if not os.path.isfile(cpath):
                raise SimError(f"no Constants.mc at {cpath}")
            _refuse_if_dirty(cpath, force)
            with open(cpath, "rb") as fh:
                original = fh.read()
            backups.append((cpath, original))
            text = original.decode("utf-8")
            pending[cpath] = text
            for name, value in consts.items():
                pat = re.compile(rf"(const\s+{re.escape(name)}\s*=\s*)([^;]+)(;)")
                if not pat.search(text):
                    raise SimError(f"const {name} not found in Constants.mc")
                # A function replacement, NOT an f-string template: in a
                # template, backslashes in the value are read as regular
                # expression escapes, so a value of `"\1"` would splice the
                # matched text back into the user's source and `"a\g"` would
                # raise. Seeds legitimately carry paths and escapes.
                text = pat.sub(
                    lambda m, _v=value: m.group(1) + _v + m.group(3), text)
            pending[cpath] = text
        for p in (patches or []):
            fpath = _seed_target(app_dir, p["file"])
            if not os.path.isfile(fpath):
                raise SimError(f"patch target not found: {p['file']}")
            if fpath not in pending:
                _refuse_if_dirty(fpath, force)
                with open(fpath, "rb") as fh:
                    orig = fh.read()
                backups.append((fpath, orig))
                pending[fpath] = orig.decode("utf-8")
            new_t, n = re.subn(p["pattern"], p["repl"], pending[fpath],
                               count=p.get("count", 1))
            if n == 0:
                raise SimError(f"patch pattern not found in {p['file']}: {p['pattern']}")
            pending[fpath] = new_t
        edits = [(path, text.encode("utf-8")) for path, text in pending.items()]

        # Every check has passed and nothing above this line touched the tree, so
        # a rejected seed can never half-apply. Journal the originals, then write.
        #
        # Writes are binary on purpose: the source was decoded as UTF-8, so it is
        # encoded back the same way rather than in whatever the locale happens to
        # be, and text mode on Windows would rewrite every newline. It also makes
        # the bytes on disk exactly what the journal hashed.
        originals = dict(backups)
        try:
            seedjournal.record([(p, originals[p], new) for p, new in edits])
        except OSError as exc:
            # Crash recovery is a safety net, not a precondition — but say so.
            log.warning("could not write the seed journal (%s); a SIGKILL during "
                        "this job would leave the patch in your tree", exc)
        try:
            for path, new_bytes in edits:
                with open(path, "wb") as fh:
                    fh.write(new_bytes)
        except Exception:
            # A failed write mid-batch — roll every touched file back.
            for path, orig in backups:
                with open(path, "wb") as fh:
                    fh.write(orig)
            seedjournal.clear()
            raise
        self._seed_backup = backups
        return {"ok": True,
                "seeded_consts": list((consts or {}).keys()),
                "patched_files": [os.path.basename(f) for f, _ in backups
                                  if not f.endswith("Constants.mc")]}

    def restore(self) -> dict:
        """Put every seeded file back byte-exact and assert git sees no change
        (never leave SEED_*=true or a stray source patch). Idempotent."""
        if self._seed_backup is None:
            return {"ok": True, "noop": True}
        restored, clean = [], True
        for path, original in self._seed_backup:
            with open(path, "wb") as fh:
                fh.write(original)
            restored.append(path)
            clean = clean and _git_clean(path)
        self._seed_backup = None
        seedjournal.clear()
        return {"ok": True, "restored": restored, "git_clean": clean}

    # ── tests ───────────────────────────────────────────────────────────────────
    TEST_RETRY_SETTLE_S = 3.0

    def run_tests(self, prg: str, device: str = "venu3",
                  timeout_s: float = 300.0) -> dict:
        """monkeydo <prg> <device> -t. Parse the CIQ harness summary into a
        structured result. This is one 'test-batch' chunk (the broker may run
        several for a full suite so preemption lands between them).

        Green needs a PASSED verdict AND a non-zero test count. SDK 9.1.0
        prints ``Ran N tests`` and ``PASSED (passed=N, failed=N , errors=N)``;
        monkeydo's exit code is 1 even on a clean pass, so it proves nothing.
        A run with no verdict is never green: "Unable to connect to
        simulator." prints no FAIL token, and counting FAIL tokens once
        reported that as a pass with 0 failures."""
        self.ensure()
        # Test PRGs get a fresh, journaled storage too: a suite that passes only
        # on what an earlier run left behind is a suite that lies.
        self._storage_begin(prg, None, "test", "fresh")
        self.current = None   # the test run replaces whatever app was up
        res = None
        for attempt in (1, 2):
            st = simport.wait_for_port(20.0)
            if not st.reachable:
                return {"ok": False, "reason": "simulator-unreachable",
                        "detail": simport.describe(st), "passed": 0, "failed": 0}
            # A listening port is not a simulator that accepts a monkeydo
            # session: on a cold start the first connection can still be
            # refused ("Unable to connect to simulator.") a moment after the
            # port appears. The load path absorbs that by polling its verify;
            # a test run has no verify, so it retries once, after a short
            # settle, before reporting the simulator unreachable.
            cmd, env, _how = simport.monkeydo_command(self.sdk, prg, device, ["-t"], st=st)
            try:
                r = subprocess.run(cmd, env=env, capture_output=True,
                                   encoding="utf-8", errors="replace",
                                   stdin=subprocess.DEVNULL, timeout=timeout_s,
                                   **hidden_console_kwargs())
                out = (r.stdout or "") + (r.stderr or "")
            except subprocess.TimeoutExpired as e:
                return {"ok": False, "reason": "timeout",
                        "out": (e.output or "")[-2000:] if isinstance(e.output, str) else ""}
            res = _parse_test_output(out)
            if res.get("reason") != "simulator-unreachable" or attempt == 2:
                break
            log.warning("monkeydo could not connect on the first test run; retrying once")
            _safe(su._kill_orphan_monkeydo)
            time.sleep(self.TEST_RETRY_SETTLE_S)
        pinned = _safe(self._storage_crash_check, None)
        if pinned:
            res["crash_evidence"] = pinned
        return res

    # ── health / teardown ───────────────────────────────────────────────────────
    def healthy(self) -> bool:
        try:
            return su.find_sim_window() is not None
        except Exception:
            return False

    def restart(self) -> dict:
        """Kill and relaunch the simulator.

        Every failure leaves here as a SimError. This is called from the
        broker's SCHEDULER thread (zombie reaping), which is guarded by
        ``except Exception``; a SystemExit escaping from the capture layer
        would kill that thread outright and leave a daemon that accepts jobs
        it will never run.
        """
        try:
            su._restart_simulator()
        except su.SimulatorLaunchError as exc:
            self.current = None
            raise SimError(f"restarting the simulator failed: {exc}") from exc
        except SystemExit as exc:
            self.current = None
            raise SimError("restarting the simulator failed (timeout)") from exc
        self.current = None
        self._rebind()
        return {"ok": True}


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return None


def _seed_target(app_dir: str, rel: str) -> str:
    """Resolve a seed target inside ``app_dir``, refusing anything that escapes.

    ``os.path.join(app_dir, rel)`` is not a containment check: an absolute
    ``rel`` discards ``app_dir`` entirely, and ``../`` walks out of it. Seeding
    is reachable from the broker socket and from the MCP server, so an unchecked
    join is a write primitive for every file the user owns. Symlinks are
    resolved first, so a link inside the project cannot point out of it either.
    """
    root = os.path.realpath(app_dir)
    full = os.path.realpath(os.path.join(root, rel))
    if full != root and not full.startswith(root + os.sep):
        raise SimError(
            f"refusing to patch {rel!r}: it resolves to {full}, outside the "
            f"project directory {root}. Seed targets must be inside the project.")
    return full


def _git_status(path: str) -> str:
    """'clean' | 'dirty' | 'untracked' | 'no-git' for one file."""
    d = os.path.dirname(path) or "."
    try:
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", "--", path],
                                 cwd=d, capture_output=True)
        if tracked.returncode != 0:
            inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                                    cwd=d, capture_output=True)
            return "untracked" if inside.returncode == 0 else "no-git"
        r = subprocess.run(["git", "diff", "--quiet", "--", path],
                           cwd=d, capture_output=True)
        return "clean" if r.returncode == 0 else "dirty"
    except OSError:
        return "no-git"   # git is not installed; the journal still covers us


def _refuse_if_dirty(path: str, force: bool) -> None:
    """Do not patch a file whose uncommitted changes we could not give back.

    Restore is byte-exact and runs on every observable exit, but a SIGKILL
    leaves the patch behind. If the file was committed, that is a
    `git checkout` away; if it held uncommitted work, the journal is the only
    copy. Refusing by default keeps the worst case boring.
    """
    if force:
        return
    if _git_status(path) == "dirty":
        raise SimError(
            f"{path} has uncommitted changes. Seeding rewrites it and restores "
            f"it afterwards, but a SIGKILL mid-job would leave the patch in "
            f"place with your edits only in ciqkit's journal. Commit or stash "
            f"first, or pass force to seed anyway.")


def _git_clean(path: str) -> bool:
    return _git_status(path) in ("clean", "untracked", "no-git")


def _flatten_overhead_gaps(wall_ms: list[int], overhead: set[int]) -> list[int]:
    """Replace frame durations that measure ciqkit, not the app.

    Frame durations come from the real capture timestamps so that a genuine
    pause on the watch still reads as a pause. But two of the gaps in a clip
    are not the watch pausing -- they are this module doing bookkeeping between
    two grabs: the ~0.5 s canvas-mask build after frame 0, and the ~1.05 s
    window re-raise before every `refocus_every`-th frame.

    At the old ~0.5 fps those hid inside a two-second frame time. At ~4.6 fps
    they do not: measured on a real clip, the refocus frame held for 1450 ms
    against a 170 ms median -- a 8x freeze punched into the playback every
    ~5 s, in a clip whose whole point is watching motion. `_compress_durations`
    cannot help, because a healthy clip is already faster than `playback_ms`
    and passes through untouched, and its [20, 1000] ms clamp only applies on
    the branch that scales.

    So flatten these gaps to the median of the honest ones. That is not a lie
    about the app: nothing happened on the watch during a window raise that the
    neighbouring frames do not already show. Real stalls -- a slow redraw, a
    blocking dialog -- are untouched and still read as stalls.
    """
    honest = [d for i, d in enumerate(wall_ms) if i not in overhead]
    if not honest or len(honest) == len(wall_ms):
        return list(wall_ms)
    ordered = sorted(honest)
    mid = len(ordered) // 2
    median = int(ordered[mid] if len(ordered) % 2
                 else (ordered[mid - 1] + ordered[mid]) / 2.0)
    # Only ever SHORTEN an overhead gap. If a gap we flagged came in under the
    # median anyway, the bookkeeping evidently cost nothing and stretching it
    # out to the median would invent a pause that never happened.
    return [min(d, median) if i in overhead else d
            for i, d in enumerate(wall_ms)]


def _compress_durations(wall_ms: list[int], playback_ms: int) -> tuple[list[int], float]:
    """Scale wall-clock frame durations into a playable GIF timing track.

    Keeps the RATIOS between frames (so a real pause still reads as a pause) but
    scales the whole track so the MEDIAN frame lasts `playback_ms`. Median, not
    mean, so one slow grab does not drag every other frame down with it.
    `playback_ms <= 0` returns the wall-clock track untouched.

    Frames are clamped to [20, 1000] ms: under 20 ms most viewers silently
    substitute 100 ms, and a single outlier over 1 s looks like the clip froze.
    """
    if playback_ms <= 0 or not wall_ms:
        return list(wall_ms), 1.0
    ordered = sorted(wall_ms)
    mid = len(ordered) // 2
    median = (ordered[mid] if len(ordered) % 2
              else (ordered[mid - 1] + ordered[mid]) / 2.0)
    if median <= 0:
        return list(wall_ms), 1.0
    scale = playback_ms / float(median)
    if scale >= 1.0:
        # Already at or slower than the requested pace -- do not stretch a clip
        # that plays fine, only compress one that does not.
        return list(wall_ms), 1.0
    return [max(20, min(1000, int(round(d * scale)))) for d in wall_ms], 1.0 / scale


_VERDICT = re.compile(r"^(PASSED|FAILED)\s*\(passed=(\d+),\s*failed=(\d+)\s*,\s*"
                      r"errors=(\d+)\)", re.M)
_LEGACY = re.compile(r"Passed[:\s]+(\d+).*?Failed[:\s]+(\d+)", re.S | re.I)
_RAN = re.compile(r"^Ran (\d+) tests?$", re.M)


def _parse_test_output(out: str) -> dict:
    """Harness text -> {ok, passed, failed, errors, ran, reason?, out}."""
    tail = out[-2000:]
    m = _VERDICT.search(out)
    if m:
        verdict = m.group(1)
        passed, failed, errors = int(m.group(2)), int(m.group(3)), int(m.group(4))
    else:
        legacy = _LEGACY.search(out)
        if not legacy:
            reason = ("simulator-unreachable" if "Unable to connect to simulator" in out
                      else "no-verdict")
            return {"ok": False, "reason": reason, "passed": 0, "failed": 0, "out": tail}
        passed, failed, errors = int(legacy.group(1)), int(legacy.group(2)), 0
        verdict = "PASSED" if failed == 0 else "FAILED"
    ran_m = _RAN.search(out)
    ran = int(ran_m.group(1)) if ran_m else passed + failed + errors
    res = {"ok": verdict == "PASSED" and failed == 0 and errors == 0 and ran > 0,
           "passed": passed, "failed": failed, "errors": errors, "ran": ran, "out": tail}
    if ran == 0:
        res["reason"] = "zero-tests"
    return res
