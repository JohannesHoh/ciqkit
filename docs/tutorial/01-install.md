# 1. Install

You need five things: Garmin's SDK, Python, ciqkit, two macOS permissions and a
developer key. `ciqkit doctor` checks all of them at the end.

## Garmin's SDK and devices

ciqkit does not ship or download any Garmin software; you install it yourself.

1. Install Garmin's **SDK Manager** from
   [developer.garmin.com/connect-iq/sdk](https://developer.garmin.com/connect-iq/sdk/)
   and sign in with a free Garmin account.
2. In the SDK Manager, download the newest **SDK**.
3. Still in the SDK Manager, download the **devices** the example targets:
   **Venu 3**, **Forerunner 255** and **Venu Sq 2**. (One device is enough to
   start; the example's default is Venu 3.)
4. Start the **Connect IQ Simulator** once from the SDK Manager to make sure it
   opens, then quit it. ciqkit starts it for you from now on.

ciqkit finds the newest installed SDK by itself. If you keep it somewhere
unusual, set `CIQ_HOME` to the SDK directory.

## Python and ciqkit

You need Python 3.10 or newer (`python3 --version`).

```bash
pip install "ciqkit[sim,ocr] @ git+https://github.com/JohannesHoh/ciqkit"
```

(ciqkit is not on PyPI yet, which is why the command names its GitHub repo.)

`sim` is what drives the simulator (screenshots, input). `ocr` lets ciqkit read
the text on a screenshot, which is how it proves a screen is the one you wanted
(`--anchor`). Without `ocr` everything still works; anchor checks are skipped.
The other extras are listed in [../install.md](../install.md#2-install-ciqkit).

## macOS permissions

ciqkit moves and reads the simulator window, so macOS must allow it. Open
**System Settings → Privacy & Security** and turn on, for **the terminal or
editor you run ciqkit from** (Terminal, iTerm, VS Code...):

* **Accessibility**, and also `/usr/bin/osascript` under Accessibility;
* **Screen Recording**, because screenshots are screen grabs.

Then quit that app completely (Cmd-Q) and reopen it; a running program does
not see a new permission. Skipping this is the most common cause of a
misleading "simulator did not start".

## A developer key

Connect IQ apps must be signed. Create a key once:

```bash
ciqkit keygen
```

It writes `~/.config/ciqkit/developer_key.der` (needs `openssl`, which macOS
has). If a key is already there it stops with an error and leaves it alone, so
running it twice is safe. Keep it private and never commit it.
Set `CIQKIT_DEV_KEY` to use a key somewhere else.

## Check everything

```bash
ciqkit doctor
```

It prints the SDK it found, whether the simulator can be driven, and which
optional pieces are installed, and exits non-zero if something is broken. Fix
what it flags before going on; the troubleshooting list in
[../install.md](../install.md#troubleshooting) covers the usual causes.

## What you learned

* ciqkit needs Garmin's SDK, installed by you, plus Python 3.10+.
* The terminal needs Accessibility and Screen Recording permission.
* `ciqkit keygen` makes your signing key and `ciqkit doctor` checks the setup.

Next: [2. Run the example](02-run-the-example.md). Reference version of this
page: [../install.md](../install.md).
