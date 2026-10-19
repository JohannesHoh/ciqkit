# 4. Work with an agent

Pages 2 and 3 were you typing commands. The same abilities are available to a
coding agent (Claude Code, or any [MCP](https://modelcontextprotocol.io)
client) as tools, so it can change code and then check its own work on a real
simulated screen.

Read [SECURITY.md](../../SECURITY.md#3-the-mcp-server-inherits-both-of-the-above)
first: an agent with these tools can build and run any project it names.

## Set it up

Projects from `ciqkit new` and `ciqkit init --write` already contain a
`.mcp.json`. The example has one too. It registers two servers:

```jsonc
{
  "mcpServers": {
    "ciqsim":     { "command": "ciqkit-mcp" },
    "garmin-sdk": { "command": "bash", "args": ["../../tools/sdk-mcp/scripts/run-server.sh"] }
  }
}
```

* **`ciqsim`** gives the agent `ciqsim_shot`, `ciqsim_shots`, `ciqsim_nav`,
  `ciqsim_test`, `ciqsim_record`, `ciqsim_doctor`, `ciqsim_status`,
  `ciqsim_bump` and `ciqsim_cancel`: the commands you just used.
* **`garmin-sdk`** answers "what is the exact signature of X?" from the SDK
  docs on your machine, plus a list of known Monkey C pitfalls. It needs a
  one-time `npm run parse-sdk` to index your SDK; see
  [../sdk-mcp.md](../sdk-mcp.md).

Heads-up on the second one: `ciqkit new` and `ciqkit init` write
`npx -y ciqkit-sdk-mcp`, which only works once that package is published to npm.
Until then use the entry above with the path adjusted to your clone of ciqkit
(for example `bash /path/to/ciqkit/tools/sdk-mcp/scripts/run-server.sh`).
The `ciqsim` half works either way.

Then start your agent in the project directory, for example:

```bash
cd ~/tictactoe
claude
```

The project's `CLAUDE.md` (the example has one; `ciqkit new` and `ciqkit init`
add one) tells the agent how to verify: run the tests, re-shoot the screens,
check the layout, look APIs up instead of guessing.

## Prompts to try

* "Make the X marks red and verify with a screenshot on every device."
* "Add a hard mode: a setting on the home screen that makes the watch unbeatable.
  Add a unit test and a `[[screens]]` entry for the new screen."
* "Why does the AI lose here?" (paste a position). A good answer reads
  `source/Ai.mc`, reproduces the game with `ciqsim_nav` and `AI_LEVEL` seeds,
  and shows evidence rather than a guess.
* "The result text looks clipped on the Forerunner. Find out why and fix it."

## What the agent should do

* Run `ciqsim_test` and `ciqsim_shots` after every change that draws something
  or handles input, on **each** device, and actually look at the PNGs.
* Use anchors: text that must be on the screen, so a wrong screen is an error.
* Look APIs up with the `garmin-sdk` tools before writing Monkey C.
* Add a `[[screens]]` entry and a `(:test)` for new behaviour.

## What it should not do

* Claim "done" because the code compiled. A build that compiles is not a screen
  that looks right.
* Commit screenshots taken without `--native`: they contain Garmin's device
  artwork. Never commit a developer key.
* Turn off or loosen checks to get a pass, or edit `Constants.mc` instead of
  using `--seed` for a one-off screenshot.
* Fire several simulator jobs by hand at once. The broker queues them, so
  there is no need.

## What you learned

* An agent gets the same commands as MCP tools through `.mcp.json`.
* The `CLAUDE.md` in the project is what makes it verify instead of guess.

Next: [5. Your own app](05-your-own-app.md).
