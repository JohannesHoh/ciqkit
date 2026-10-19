# Static checks

Three checks that need no simulator and no SDK. They run anywhere Python,
Pillow and numpy do — including Linux CI, on screenshots captured on a Mac.

```bash
pip install "ciqkit[checks]"
```

**All three are advisory.** `layout` and `shot-score` produce a 0–100 score and
exit 0 unless you pass `--fail-under`. Distinguishing clipped text from an
intentional rim arc is not reliably possible from pixels alone, so treat a low
score as "look at this one", not "this is broken". The strongest real signal is
a score that *drops* against a screen's own baseline.

---

## `ciqkit layout` — does the content fit the screen?

Monkey C compiles regardless of where things land, so a label that runs off a
round display is not a build error. This checks the geometry of a captured
native face PNG.

```bash
ciqkit layout screenshots/                    # all PNGs in a directory
ciqkit layout home.png --round                # force round geometry
ciqkit layout screenshots/ --fail-under 80    # exit 1 if any score is lower
```

| Finding | Meaning |
| --- | --- |
| `CLIP` | ink jammed against or over the round bezel — text getting cut |
| `CORNER` | ink outside the screen circle entirely — drawn into the dead corners |
| `BAND` | ink outside the round-safe inscribed square |

Round versus rectangular is inferred from the image (square with dark corners
means round) or forced with `--round` / `--rect`.

Decorative full rings are **not** flagged: edge ink that wraps most of the way
around the rim (≥70% angular coverage) is treated as an intentional progress
arc, while edge ink concentrated in a few arcs is treated as a likely text clip.
That is what keeps the false-alarm rate usable.

Operates on the **native face crop** — the screen only. Capture with
`ciqkit sim shot --native` to get input this check understands.

Framed, cover, hero and icon assets are skipped, because their face is inset
and the geometry does not apply. **The filter is by filename**: a file whose
name contains `framed`, `cover`, `hero`, `icon`, `store-listing`, `_debug` or
`debug-` is ignored. That is worth knowing in both directions — a native crop
called `icon-home.png` is silently skipped, and a framed shot with an unusual
name is checked as if it were native and scores badly. Pass the files
explicitly if your naming differs.

---

## `ciqkit shot-score` — is this a good screenshot?

Catches the ways a capture goes wrong without looking obviously wrong in a file
listing.

```bash
ciqkit shot-score screenshots/
ciqkit shot-score screenshots/ --json
ciqkit shot-score screenshots/ --fail-under 70
ciqkit shot-score shots/ --allow-size 320x320
```

Checked:

1. The file loads as an image and is not 1×1 or absurdly large.
2. The size matches a known watch-screenshot size — the five round face sizes
   seen most often, framed store sizes, the store cover and the device icon,
   each also accepted at 2x/3x for HiDPI captures. This is **not** a complete
   list of Garmin display sizes, so an unknown size only warns; whitelist yours
   with `--allow-size WxH`.
3. Aspect ratio is 1:1 within ±5%.
4. **Colour diversity.** A flat blue frame almost always means the simulator had
   not loaded the `.prg` yet — Connect IQ shows a solid loading triangle before
   app pixels arrive. This is the single most common bad capture.
5. **Sharpness** (Laplacian variance) — a blurred grab usually means the window
   was captured mid-resize.
6. **Watch face presence.** For native crops the dark circular display must fill
   the frame; for framed shots the dark body must sit on the dark background
   with contrast between them. Uses `HoughCircles` when OpenCV is installed
   (`pip install "ciqkit[cv]"`), with a centred-darkness fallback otherwise.
7. **Framed background.** Corners should be the dark store background; bright
   corners mean the simulator's white window chrome leaked into the output.

`--json` emits one record per image for scripting.

---

## `ciqkit icon` — launcher icons

```bash
ciqkit icon beta   resources/drawables/launcher_icon.png build/beta_icon.png
ciqkit icon unique .                       # compare against sibling projects
ciqkit icon unique . ../other-project ...  # or against an explicit list
```

**`beta`** composites a diagonal BETA ribbon, scaled to ~30% of the icon width so
it stays readable from a 40×40 launcher tile to a 512×512 store asset. Composite
at *build* time onto a clean icon — never commit an icon that already has a
ribbon, or beta builds get two.

**`unique`** exits 1 if `resources/drawables/launcher_icon.png` is byte-identical
to another project's. New projects are routinely bootstrapped by copying a
working icon so the resource compiler has a decodable PNG; that placeholder is
invisible day to day and then ships, leaving two of your apps
indistinguishable in the launcher and on their store pages.

Wire it into your release build:

```bash
ciqkit icon unique "$PROJECT_DIR" || exit 1
```

---

## In CI

```yaml
- run: pip install "ciqkit[checks] @ git+https://github.com/JohannesHoh/ciqkit"
- run: ciqkit layout screenshots/ --fail-under 75
- run: ciqkit shot-score screenshots/ --fail-under 70
- run: ciqkit icon unique .
```

Capturing the screenshots still needs a Mac with the simulator; the checks do
not.
