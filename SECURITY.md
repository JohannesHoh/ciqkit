# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub private vulnerability
reporting: open the repository's **Security** tab and choose **Report a
vulnerability** (or use
[this link](https://github.com/JohannesHoh/ciqkit/security/advisories/new)).
Do not open a public issue.

This is a small project maintained by one person in their spare time. Expect an
acknowledgement within roughly a week. There is no bug bounty.

## Supported versions

Only the latest release receives fixes. There are no long-term support
branches.

## Threat model — what ciqkit assumes

Read this before running ciqkit, because two of its design points are only safe
under assumptions you should confirm hold for you.

### 1. The broker's control socket is an execution channel

`ciqkit sim` talks to a background broker over a local control channel — a
unix domain socket on macOS and Linux, authenticated loopback TCP on Windows
(see below). A job submitted on that channel can:

* run the target project's build command (`./scripts/build.sh` by default);
* patch constants and apply regular-expression edits to files inside the target
  project;
* drive mouse and keyboard input into the simulator window.

Anyone who can write to that socket can therefore run code as you. ciqkit
defends this by keeping the socket in a private per-user directory
(`$XDG_RUNTIME_DIR/ciqkit` or `<tmp>/ciqkit-<uid>`) created with mode `0700`
and verified to be owned by you, and by creating the socket itself mode `0600`.
If that directory exists but is owned by someone else or is group/world
accessible, ciqkit refuses to start rather than using it.

**On Windows**, CPython has no unix sockets, so the broker listens on an
ephemeral port on `127.0.0.1` — which every local account, and every web page
via the browser, can connect to. Connecting is therefore not enough: the first
line on a connection must carry a 256-bit token, generated fresh by each
broker and written only to `ciqsimd.endpoint` in the runtime directory
(`%LOCALAPPDATA%\ciqkit\runtime`). That directory gets a *protected* ACL
granting full control to your account and SYSTEM only — nothing inherited, no
other user, no Administrators group — and is re-checked (owner must be you) and
re-tightened on every use, the Windows equivalent of `0700`. A connection that
does not present the token within 10 seconds is answered `unauthorized` and
closed before any request on it is parsed; the 10 seconds are one deadline for
the whole line, so trickling bytes does not extend it. Until it has presented
the token a connection holds none of the broker's 64 handler slots: it waits
in a pool of 32 that, when full, drops its *oldest* member, so connections
parked by another local account cannot lock your client (which sends the token
the moment it connects) out. An account racing to open connections faster
than your client can send ~80 bytes could still delay it; that is a local
denial of service, not access. Request lines are capped at 1 MiB. Before a
client sends the token it checks that the broker which wrote
`ciqsimd.endpoint` is still running and is the process listening on that port,
so the file a hard-killed broker leaves behind does not hand the token — or
your jobs — to whoever took the port since. The check identifies the broker
by pid and port, so a process that is issued the dead broker's pid *and*
holds its port still passes; delete a leftover `ciqsimd.endpoint` after a
crash if that worries you. Anyone who can read your private
profile directory can drive the broker — exactly as on POSIX, where anyone who
can traverse your `0700` directory can.

This means:

* **Do not** point `CIQSIM_SOCK` at a shared directory such as bare `/tmp`
  (or, on Windows, a shared folder: the endpoint file *is* the key).
* **Do not** relax the permissions on the runtime directory.
* On a shared machine, each user gets their own broker. That is intentional.

### 2. Pointing ciqkit at a project runs that project's code

`ciqkit sim shot`, `record`, `nav` and `test` will build the project you point
them at if you do not pass an already-built `--prg`. Building runs a script
from that project. Treat `ciqkit sim --project X` with exactly the same caution
as `make` in an unfamiliar checkout: only run it against projects you trust.

If you want to avoid this entirely, build the `.prg` yourself and always pass
`--prg`; ciqkit then never executes anything from the project or from the
directory you run it in. In particular:

* The broker is started with `python -P` from the private runtime directory,
  so a `json.py` (or `numpy/`, `ciqkit/`) in the current directory is not
  imported into it. Its interpreter is `CIQKIT_PYTHON`, the one running
  ciqkit, or the venv ciqkit is installed in — never a `.venv` found in or
  above the current directory. A `.venv` above ciqkit's own install is used
  only if it is yours and not writable by anyone else.
* git, which ciqkit asks for the sha and dirty state of the checkout a `.prg`
  or a seeded file lives in, runs with the repository's executable settings
  overridden (`core.fsmonitor`, `core.hooksPath`, every `filter.<driver>`,
  external diff and textconv, lazy-fetch transports; a partial clone is not asked at all on git before 2.44, which ignores the switch that stops its lazy fetch). A repository whose
  filter driver name cannot be overridden is not asked at all.
* `git` and `java` are looked up only in the absolute entries of `PATH`, never
  in the current directory (Windows otherwise searches it first).

### 3. The MCP server inherits both of the above

`ciqkit-mcp` exposes the broker to an MCP client, and every tool takes a
`project` path chosen by the caller. An MCP client that can call these tools
can build any project it can name. Only enable it for clients you control.

## What ciqkit does not do

* It does not collect telemetry, phone home, or make any network request.
* It does not read, store, or transmit credentials of any kind.
* No code here talks to Garmin's online services or the Connect IQ developer
  portal.

See [docs/scope.md](docs/scope.md) for why that kind of tooling stays out.

## Seeding and your working tree

`--seed` modifies files in your project (for example, to force a screen for a
screenshot) and restores them afterwards from a byte-exact backup. The broker
restores on job completion, on failure, on `SIGTERM`/`SIGINT`, and via an
`atexit` hook.

Because a seed arrives over the control channel — and is therefore reachable
from the MCP server — it is bounded in two ways:

* **targets must stay inside the project directory.** Paths are resolved with
  symlinks followed and then checked for containment, so an absolute path or a
  `../` escape is refused rather than quietly writing wherever it points.
* **files with uncommitted changes are refused** unless the caller passes
  `force`. That is the case where a leftover patch could cost you work.

(On Windows, Ctrl-Break is handled the same way.)
`SIGKILL` — or Task Manager's "End task" — cannot be caught, so the originals
are also written to ciqkit's
private runtime directory before anything is modified. The next broker start —
or `ciqkit sim restore` — puts them back. That recovery is digest-guarded: it
writes over a file only while it still holds exactly the bytes the seed wrote,
so it cannot discard an edit you made in the meantime. Anything it declines to
touch is reported by `ciqkit doctor` and `ciqkit sim restore`, with the path to
the saved original.

## Killing the simulator

When a push fails to produce a loaded app, the broker escalates to killing and
relaunching the simulator (`pkill -x -u <you> simulator` on macOS; on Windows,
`simulator.exe` processes in your own logon session). If you had the simulator
open for your own debugging, that session goes with it. Process kills — this
one and the cleanup of stale `MonkeyDoDeux` java processes before a push — are
scoped to your own uid on POSIX and your own logon session on Windows.

On Windows the broker also dismisses error dialogs before a push and a
capture. Only dialogs belonging to the simulator process are touched; other
applications' windows never are.
