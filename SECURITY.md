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
closed before any request on it is parsed. Anyone who can read your private
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
`--prg`; ciqkit then never executes anything from the project.

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
