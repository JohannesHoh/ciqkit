# 2. Run the example

`examples/tictactoe` is a small, complete watch app (you against the watch,
tic-tac-toe) set up for ciqkit. You will build it, test it and screenshot it.

## Get the example

```bash
git clone https://github.com/JohannesHoh/ciqkit
cp -r ciqkit/examples/tictactoe ~/tictactoe
cd ~/tictactoe
```

Copying it out is optional but keeps your changes away from the clone. (If you
copy it, its `.mcp.json` points at `../../tools/sdk-mcp`; that matters in
[page 4](04-with-an-agent.md).) Without git, `ciqkit new tictactoe --template
tictactoe` creates the same app from the copy that ships inside ciqkit, with a
fresh application id and an `.mcp.json` that starts the SDK server with `npx`. Before you ever put a changed copy on a watch,
give it its own application id; see [page 5](05-your-own-app.md#the-app-id).

## Check the project

```bash
ciqkit doctor --project .
```

On top of the environment check, this looks at the project: its manifest, that
the devices it lists are installed, your developer key, which build route will
be used, and whether tests exist. There is no build script in this project, so
ciqkit builds with `monkeyc` from your SDK itself. Devices you did not install
show up here, not as a confusing build error later.

## Run the unit tests

```bash
ciqkit sim test
```

ciqkit builds a test version of the app, runs it in the simulator and prints
the result as JSON. The example has 13 unit tests for the rules and the AI. The
first command after a while is slow: the simulator has to start.

## Take your first screenshot

```bash
ciqkit sim shot home --native --anchor toe --out-dir shots
```

What happens: build, start the simulator, load the app, **verify the app really
replaced the watch face that was showing**, check that the text `toe` is on the
screen (that is the `--anchor`, read with OCR), and save `shots/home.png`. Open
it.

* `--native` saves only the watch screen. Without it you get the screen
  composited into a picture of the watch, which uses Garmin's artwork: fine to
  look at, never commit it.
* Exit code 3 means the anchor text was not found: you are not looking at the
  screen you think you are.

## Every screen at once

`ciqkit.toml` in the project names four screens (`home`, `mid-game`, `you-win`,
`draw`), each with the button presses to reach it:

```bash
ciqkit sim shots --all --native --out-dir shots
```

One JSON line per screen; open `shots/*.png`. Try another device with
`--device fr255` or `--device venusq2`. Known limit: on the rectangular
`venusq2` the screenshots are currently square crops that cut off roughly 40 px
at the top and bottom.

## What you learned

* `ciqkit doctor --project .` checks the project, `ciqkit sim test` runs its
  unit tests, `ciqkit sim shot` / `shots --all` capture verified screens.
* `--anchor TEXT` turns "a PNG appeared" into "this screen says what I expect".
* Use `--native` for anything you might commit.

Next: [3. Change it and verify](03-change-and-verify.md). More on the
commands: [../sim.md](../sim.md), [../config.md](../config.md).
