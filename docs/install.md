# Install and first run

Reference version. If you are new to Connect IQ, the step-by-step
[tutorial](tutorial/01-install.md) covers the same ground with more hand-holding.

## 1. Install the Connect IQ SDK

ciqkit does not bundle, download, or install any Garmin software. Get the SDK
from Garmin's [SDK Manager](https://developer.garmin.com/connect-iq/sdk/) and
install at least one SDK version and one device.

ciqkit finds the newest installed SDK automatically under:

| Platform | Location |
| --- | --- |
| macOS | `~/Library/Application Support/Garmin/ConnectIQ/Sdks` |
| Windows | `%APPDATA%/Garmin/ConnectIQ/Sdks` |

Set `CIQ_HOME` to an SDK directory to override the search — the same variable
Garmin's own tooling uses.

## 2. Install ciqkit

```bash
pip install "ciqkit[sim] @ git+https://github.com/JohannesHoh/ciqkit"
```

ciqkit is not on PyPI yet, so it installs from GitHub; add extras inside the
brackets the same way. Once it is published, `pip install "ciqkit[sim]"` will do.

| Extra | Pulls in | For |
| --- | --- | --- |
| `sim` | Pillow, numpy, pyautogui | driving the simulator (capture + input) |
| `checks` | Pillow, numpy | `layout`, `shot-score`, `icon` only — no GUI |
| `ocr` | ocrmac (macOS), winocr (Windows) | verifying on-screen text with `--anchor` |
| `cv` | opencv-python-headless | sharper watch-face detection in `shot-score` |

With no extras, only `ciqkit lease`, `ciqkit storage` and `ciqkit doctor` are
useful. That is intentional: the base install has zero dependencies.

On Windows, quote the extras with double quotes as above: single quotes are
passed to pip literally by `cmd.exe` and PowerShell.

## 3. macOS: grant Accessibility permission

ciqkit positions and reads the simulator window through AppleScript. Without
the Accessibility grant, every one of those calls fails with error `-25211`,
window lookup returns nothing, and the symptom is a misleading
`simulator did not start`.

**System Settings → Privacy & Security → Accessibility**, then enable **both**:

* the terminal or editor you run ciqkit from (Terminal, iTerm, VS Code, …), and
* `/usr/bin/osascript`

Then fully quit and reopen that app — a running process does not pick up a new
grant.

## 3. Windows

No permission prompt is involved. What is needed:

* **Java.** Every push and test run is a Java process (the SDK's
  `bin\monkeydo.bat` wraps one), so `java -version` must work in a new
  terminal. `ciqkit doctor` fails without it.
* **Git for Windows**, if your build command is a shell script — the default
  `./scripts/build.sh -d <device>` is. ciqkit runs `.sh` scripts with the bash
  that ships with Git (found next to `git.exe`, or set `CIQKIT_BASH`). It never
  uses the `bash.exe` Windows puts on `PATH`, which is WSL's: it would build
  inside a Linux VM that cannot see the Windows SDK. Without Git Bash, a
  `scripts\build.cmd`, `.bat` or `.ps1` beside `build.sh` is used instead, or
  set `CIQKIT_BUILD_CMD` (Windows paths with backslashes are fine), or pass
  `--prg`.
* **An interactive, unlocked desktop for gestures.** Taps, swipes and keys are
  real input, which Windows discards while the session is locked. Screenshots,
  recordings, loads and test runs keep working when it is locked.
* **The simulator's Touch Screen setting** (Settings menu) must be on for taps
  and swipes to do anything. Button keys work either way. `ciqkit doctor`
  reports it.

High-DPI displays are handled: the simulator is drawn scaled by Windows, and
ciqkit captures it at the watch's native pixel size and converts every
gesture back to screen pixels. If the simulator window hangs below the
taskbar, ciqkit moves it up before a gesture.

## 4. Verify

```bash
ciqkit doctor
```

Expected output on a working machine:

```
ciqkit 0.1.0  (Darwin 24.x, python 3.12.x)
runtime dir      /var/.../ciqkit-501  (private, 0700)
Connect IQ SDK   /Users/you/Library/.../connectiq-sdk-mac-9.1.0-...
simulator        preflight OK
Pillow           installed
numpy            installed
...
```

`ciqkit doctor` exits non-zero if something is actually broken, so it works as
a CI or setup gate.

## Troubleshooting

**`No Connect IQ SDK found`**
The SDK Manager has not installed an SDK, or it went somewhere unusual. Set
`CIQ_HOME=/path/to/connectiq-sdk-...`.

**`macOS Accessibility permission is missing`**
See step 3. If you already granted it, quit the terminal app completely
(⌘Q, not just closing the window) and reopen.

**`simulator did not start (timeout)`**
macOS: almost always one of the Accessibility grant (above), or running over
SSH / VNC / Screen Sharing. The simulator needs a local WindowServer and aborts
on a headless session — run it on the physical Mac console.
Windows: it needs a logged-in desktop — not a service, and not a scheduled task
set to run whether or not the user is logged on.

**`refusing to listen in <dir>`**
The runtime directory exists but is owned by another user or (POSIX) is
group/world accessible and could not be tightened. That is the guard described
in
[SECURITY.md](../SECURITY.md#1-the-brokers-control-socket-is-an-execution-channel).
Remove the directory, or point `XDG_RUNTIME_DIR` at one you own.

**`the Windows session is locked, so taps, swipes and keys cannot reach the simulator`**
Exactly that: unlock it. Shots and recordings do not need it unlocked; `nav`
and gestures do.

**A tap or swipe "did not change the screen" on Windows**
Check the simulator's Settings → Touch Screen (`ciqkit doctor` warns when it is
off), and that no always-on-top window covers the watch.

**`... is a shell script, and there is no Git for Windows bash`**
Install Git for Windows, set `CIQKIT_BASH` to its `bash.exe`, add a native
`scripts\build.cmd`, set `CIQKIT_BUILD_CMD`, or pass `--prg`.

**`pyautogui is required to send input to the simulator`**
You installed the `checks` extra but are trying to drive the simulator. Install
`ciqkit[sim]`.

**A job hangs, or the queue looks stuck**

```bash
ciqkit sim status        # what is running, what is queued, ETAs
ciqkit sim wait <id>     # re-attach to a job whose command timed out
ciqkit sim cancel <id>   # drop one job
ciqkit sim shutdown      # stop the broker; it restarts on the next command
```

None of these start a broker: `ciqkit sim status` on a quiet machine tells you
nothing is running rather than booting a daemon to say so.

A command that prints `wait timeout` has **not** cancelled anything — the job
keeps running and still writes its output. Collect it with `ciqkit sim wait
<id>`; finished jobs stay answerable for 15 minutes
(`CIQSIM_JOB_RETENTION_S`).

The broker also exits on its own after 10 minutes idle
(`CIQSIM_IDLE_EXIT_S`).

## Environment variables

| Variable | Effect |
| --- | --- |
| `CIQ_HOME` | Use this SDK instead of auto-detecting |
| `CIQKIT_BUILD_CMD` | Build command run in the project dir; `{device}` is substituted. Default: `./scripts/build.sh -d {device}` if the project has one, else the built-in `monkeyc` build (`-f monkey.jungle -d DEVICE -y KEY -o bin/<project-dir>.prg`) |
| `CIQKIT_DEV_KEY` | Developer key (.der) the built-in build signs with. Default `$XDG_CONFIG_HOME/ciqkit/developer_key.der` (`~/.config/...`); create one with `ciqkit keygen` (openssl, RSA 4096, mode 0600, never overwrites) |
| `CIQKIT_BASH` | Windows: the bash that runs `.sh` build commands and `ciqkit lease` scripts (default: Git for Windows' bash) |
| `CIQKIT_OCR_LANG` | Windows OCR language for `--anchor` (default `en`; needs that Windows OCR language pack) |
| `CIQSIM_SOCK` | Broker socket path (on Windows: its endpoint file). Only override if you understand [SECURITY.md](../SECURITY.md) |
| `CIQSIM_TRANSPORT` | `tcp` forces the Windows control channel on POSIX too (used by the test suite) |
| `CIQSIM_IDLE_EXIT_S` | Seconds of idleness before the broker exits (default 600) |
| `CIQSIM_JOB_RETENTION_S` | How long a finished job stays visible in `sim status` and answerable by `sim wait` (default 900) |
| `CIQSIM_ZOMBIE_GRACE_S` | How long to wait for a timed-out chunk's worker to stop before running the next job anyway (default 60) |
| `CIQKIT_SESSION_LABEL` | Name shown as the holder in `ciqkit sim status` and `ciqkit lease status` |
| `XDG_RUNTIME_DIR` | Parent of the private runtime directory, when set (default: `<tmp>/ciqkit-<uid>`; Windows `%LOCALAPPDATA%\ciqkit\runtime`) |
| `CIQKIT_LOG_LEVEL` | Broker log level (default `INFO`). `DEBUG` adds the face detector's per-frame reasoning — the first thing to turn on when a screenshot comes out wrong. |

ciqkit never writes diagnostics to stdout: the CLI prints JSON there, and the
MCP server speaks JSON-RPC over it. Everything else goes to the broker log at
`<runtime dir>/brokerd.log`.
