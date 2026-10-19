# 3. Change it and verify

The point of ciqkit: you change something, and the screenshot proves it. Work
in your copy of the example (`cd ~/tictactoe`).

## A baseline first

```bash
ciqkit sim shots --all --native --out-dir before
```

## Change a colour

The drawing lives in `source/GameView.mc`. The colours are constants at the top:

```
const COLOR_X = 0x00AAFF;
```

Change `COLOR_X` to red (`0xFF0000`; Monkey C colours are `0xRRGGBB`). The same
constant also colours the "you won" text and your win count on the home screen,
so those change too. Then re-shoot:

```bash
ciqkit sim shots --all --native --force --out-dir after
```

`--force` matters here. ciqkit skips re-pushing an app it thinks is already
loaded, so after you rebuild the same project it would show the previous
build's screen. Open `before/mid-game.png` and `after/mid-game.png` side by
side: the X marks should now be red.

## Change the AI

The opponent's strength is a constant in `source/Constants.mc`:

```
const AI_LEVEL = 2;
```

`0` always takes the first free cell, `1` takes a winning move, `2` also blocks
yours, `9` cannot be beaten. You can edit the file, or have ciqkit change it for
one run only. A seed patches the constant, runs the job, and restores the file
byte for byte:

```bash
ciqkit sim shot weak-ai --seed AI_LEVEL=0 --native --out-dir after
ciqkit sim shot watch-first --seed FIRST_PLAYER=WATCH --native --out-dir after
```

That is also how the `you-win` and `draw` screens in `ciqkit.toml` are
reproducible: `AI_LEVEL=0` makes every game go the same way. Seeding refuses to
touch a `Constants.mc` with uncommitted git changes, so commit (or stash) your
edits first.

Then run the tests again; they cover the AI levels:

```bash
ciqkit sim test
```

## Check the layout

```bash
ciqkit layout --round after/*.png
```

It flags content clipped by the round bezel, in the dead corners, or
overlapping. The score is advisory: the most useful signal is a score that
*drops* compared with the screen's earlier one. For the rectangular device use
`--rect`. Add `--fail-under 80` to make it exit non-zero below that score.

## What you learned

* Capture before and after, then compare the PNGs. Use `--force` after
  rebuilding.
* `--seed NAME=VALUE` changes a constant for one job only.
* `ciqkit layout` catches clipped content; `ciqkit sim test` guards behaviour.

Next: [4. Work with an agent](04-with-an-agent.md).
