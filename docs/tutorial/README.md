# Tutorial: from zero to a verified watch app

For people who are new to Garmin Connect IQ, or new to using the simulator
from the command line. Every page has commands you can copy and ends with what
you learned and where to go next. About an hour in total, most of it
installing.

Do the pages in order:

1. [Install](01-install.md) — Garmin's SDK, ciqkit, the macOS permissions, `ciqkit doctor`.
2. [Run the example](02-run-the-example.md) — build, test and screenshot the tic-tac-toe app.
3. [Change it and verify](03-change-and-verify.md) — change a colour and the AI, and prove the change on screen.
4. [Work with an agent](04-with-an-agent.md) — let Claude Code (or any MCP client) do that loop for you.
5. [Your own app](05-your-own-app.md) — `ciqkit new` for an app, watch face or data field.

Already have a Connect IQ project? Skip to [Adopting ciqkit in an existing project](../adopt.md).

The simulator side needs **macOS**. On Linux you can still run the static checks
(`ciqkit layout`, `ciqkit shot-score`) on screenshots made elsewhere.
