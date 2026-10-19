# Tic-Tac-Toe — a ciqkit example app

A small but complete Connect IQ watch app: you against the watch on a 3×3
grid. It exists to show what a project set up for ciqkit looks like, and to
give you something real to build, screenshot, test and change.

* Works with **touch and with buttons** on every device it targets.
* The watch plays minimax with a depth limit you can set (`AI_LEVEL`).
* Wins, ties and losses are kept across launches in `Application.Storage`.
* Unit tests for the rules and the opponent (`source/tests/`).
* Named screens in `ciqkit.toml` that `ciqkit sim shots --all` regenerates.

> **Forking this?** Change the application `id` in `manifest.xml` first
> (`python3 -c "import uuid; print(uuid.uuid4())"`). Two apps with the same id
> replace each other on a watch and clash in the Connect IQ store.

## Controls

| | Buttons | Touch |
|---|---|---|
| Start a game / play again | START | tap anywhere |
| Move the cursor | UP / DOWN | swipe up / down |
| Place your X | START | tap the cell |
| Back to the home screen, then exit | BACK | swipe right / BACK |

The cursor skips cells that are already taken, and after the watch moves it
jumps to the first free cell if its own cell was taken.

## Devices

The manifest lists three devices, chosen to cover the cases that break layouts
and input handling:

| Device | Why |
|---|---|
| `venu3` | Round AMOLED, 454×454, **touch**, only START and BACK buttons. The default device (first in the manifest, and `[sim] device` in `ciqkit.toml`). |
| `fr255` | Round MIP (memory-in-pixel, 64 colours), 260×260, **no touch screen**, five buttons. Proves the app is fully playable on buttons alone. (`fr265` and `fenix7` were the first candidates, but both have touch screens.) |
| `venusq2` | **Rectangular** AMOLED, 320×360, touch, two buttons. Proves the layout does not assume a circle. |

Adding a device: add an `<iq:product>` to `manifest.xml`, install the device
in the SDK Manager, and check its launcher icon size (the compiler warns when
it has to scale the icon; see `art/make_icon.py`).

## Run it with ciqkit

New to this? [Tutorial page 2](https://github.com/JohannesHoh/ciqkit/blob/main/docs/tutorial/02-run-the-example.md) walks through
the commands below step by step.

You need the Connect IQ SDK and devices installed through Garmin's SDK Manager,
and ciqkit installed with the simulator extra (`pip install "ciqkit[sim]"`).
Then, from this directory:

```bash
ciqkit keygen                            # once: creates your developer key
ciqkit doctor --project .                # checks SDK, devices, key, build, tests

ciqkit sim shot home --native --anchor toe       # build, load, screenshot
ciqkit sim shots --all --native --out-dir shots  # every [[screens]] entry
ciqkit sim test                                  # unit tests (builds the -t variant)
ciqkit layout --round shots/*.png                # clipped or overflowing content?
```

`--native` saves just the watch face. Without it you get the face composited
into the device image from your SDK, which is fine to look at but is Garmin's
artwork, so never commit or publish those.

Other devices: add `--device fr255` (or `venusq2`) to any `sim` command.

### Building

There is deliberately no `scripts/build.sh`. Without one, ciqkit builds the
app itself inside every `sim` job: `monkeyc` from your installed SDK, your
developer key from `ciqkit keygen`, output in `bin/tictactoe.prg` (and
`bin/tictactoe-test.prg` for `sim test`). One less file to understand, and
nothing to keep in sync with the SDK path.

To compile without the simulator, call `monkeyc` directly (it is in the SDK's
`bin/` directory):

```bash
monkeyc -f monkey.jungle -d venu3 -y ~/.config/ciqkit/developer_key.der -o bin/tictactoe.prg
monkeyc -f monkey.jungle -d venu3 -y ~/.config/ciqkit/developer_key.der -o bin/tictactoe-test.prg -t
```

If you want your own build script later, add it and tell ciqkit about it in
`ciqkit.toml` (`[build] cmd` and `test_cmd`).

## Screens and seeds

`ciqkit.toml` names four screens: `home`, `mid-game`, `you-win` and `draw`.
Each is a list of button presses plus, for the game-over screens, **seeds**:
`source/Constants.mc` values that ciqkit patches for one job and then restores
byte-for-byte. `AI_LEVEL = 0` makes the watch always take the first free cell,
so every game goes the same way and the screenshot is reproducible.

```bash
ciqkit sim shot weak-ai --seed AI_LEVEL=0 --native
ciqkit sim shot watch-first --seed FIRST_PLAYER=WATCH --native
```

Seeding refuses to touch a `Constants.mc` with uncommitted changes, so commit
(or stash) your edits first.

## Layout of the code

```
manifest.xml            app id, devices, launcher icon
monkey.jungle           build config (everything under source/, tests included)
ciqkit.toml             default device and named screens
source/
  Constants.mc          tunables: AI_LEVEL, FIRST_PLAYER, CURSOR_START
  Board.mc              rules: win/draw detection (pure functions)
  Ai.mc                 the watch's opponent: negamax + alpha-beta, depth-limited
  Game.mc               state machine: home -> playing -> result
  Score.mc              won / tied / lost in Application.Storage
  GameView.mc           drawing, sized from the screen, round or rectangular
  GameDelegate.mc       buttons, taps and swipes -> Game
  TicTacToeApp.mc       entry point
  tests/                (:test) unit tests, compiled only with monkeyc -t
resources/              strings and the 70×70 launcher icon
resources-fr255/        40×40 launcher icon for fr255
resources-venusq2/      40×40 launcher icon for venusq2
art/make_icon.py        draws the launcher icons (needs Pillow)
```

## Working on it with an AI agent

`.mcp.json` registers two MCP servers for agents such as Claude Code: `ciqsim`
(screenshots, navigation and unit tests through the ciqkit broker) and
`garmin-sdk` (API lookup over your local SDK docs, plus a list of known Monkey C
pitfalls). The `garmin-sdk` entry runs the server from this repository's
`tools/sdk-mcp`, so it works while the example lives inside the ciqkit checkout;
if you copy the example elsewhere, point it at your own checkout.
`CLAUDE.md` tells the agent how to verify its changes.

## Licence

[MIT No Attribution](../LICENSE): copy, change and ship it without keeping
any notice. The launcher icon is original artwork drawn by
`art/make_icon.py`. Nothing in this directory comes from Garmin's SDK, samples
or device files.
