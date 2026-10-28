# Release checklist (maintainers)

What has to happen, in order, to take ciqkit from a private repository to a
public 0.1.0 on GitHub, PyPI and npm. Every step here is outward-facing or
needs a real machine, so none of it is automated. Tick the steps in a copy of
this file or in the release issue.

The repo is at `github.com/JohannesHoh/ciqkit` throughout. Change it everywhere
(`pyproject.toml`, `tools/sdk-mcp/package.json`, SECURITY.md, the issue
templates, `docs/tutorial/02-run-the-example.md`, CONTRIBUTING.md) if the
public home is different.

## A. Dry run as a fresh macOS user

The test suite proves the logic. It cannot prove that a stranger can install
this and get a screenshot out. Do this on a clean machine, or in a **new macOS
user account** (System Settings → Users & Groups), which starts with no
permissions, no `~/.config/ciqkit`, no SDK and no venvs. Write down every
place you stall.

1. Build the artefacts you are about to publish, from the release commit:
   ```bash
   python -m pip install --upgrade build
   python -m build                      # dist/ciqkit-0.1.0-py3-none-any.whl + sdist
   (cd tools/sdk-mcp && npm ci && npm pack)   # ciqkit-sdk-mcp-0.1.0.tgz
   ```
   Copy `dist/*.whl` and the `.tgz` somewhere the test user can read.
2. As the test user, follow [docs/tutorial/01-install.md](tutorial/01-install.md)
   literally:
   * install Garmin's SDK Manager, sign in, download the newest SDK and the
     devices venu3, fr255 and venusq2; start the simulator once and quit it;
   * install Python 3.11+ (python.org installer or Homebrew);
   * install ciqkit **from the wheel**, not from PyPI and not editable:
     `pipx install "/path/ciqkit-0.1.0-py3-none-any.whl[sim,ocr]"` (or a
     venv plus `pip install "/path/…whl[sim,ocr]"`);
   * grant **Accessibility** (the terminal app and `/usr/bin/osascript`) and
     **Screen Recording** to the terminal, then quit and reopen it;
   * `ciqkit keygen`, then `ciqkit doctor` (must exit 0).
3. Tutorial 02–05 end to end, with the simulator:
   * 02: get the example (clone, or `ciqkit new tictactoe --template
     tictactoe`), `ciqkit doctor --project .`, `ciqkit sim test` (13 tests
     pass), `ciqkit sim shot home --native --anchor …`, `ciqkit sim shots --all
     --native --out-dir shots`;
   * 03: change a colour and the AI level, re-shoot, `ciqkit layout`;
   * 04: register the MCP servers (`ciqkit-mcp`, and the SDK server from the
     packed `.tgz`: `npm i -g ./ciqkit-sdk-mcp-0.1.0.tgz`, `ciqkit-sdk-parse`)
     and run one of the example prompts in a coding agent;
   * 05: `ciqkit new myapp` for `--type app`, `watchface` and `datafield`;
     `ciqkit sim shot home --native` on each.
4. Journey 2 on a **real existing project** you have not touched with ciqkit
   (a copy of it): follow [docs/adopt.md](adopt.md): `ciqkit doctor
   --project`, `ciqkit init` (read the diff), `ciqkit init --write --ci`, a
   first shot per screen, `ciqkit sim test` (with `[build] test_cmd` if the
   project has its own build script).
5. Things to look at on the way: does the first `sim` command explain itself
   while the simulator starts? Does a missing permission produce a message
   that names the fix? Does anything write outside `~/.config/ciqkit`, the
   project and the private runtime dir?
6. Run `python3 scripts/sim_smoke.py` (from the dev venv) on the macOS and
   the Windows desktop and commit the lines it appends to
   `docs/sim-smoke.jsonl`. It drives every public session op once through the
   broker on a real simulator; the test suite only ever drives a fake. Every
   op must pass, or be skipped with a reason the platform explains;
   `python3 scripts/sim_smoke_reading.py` should read 1.0 on macOS.
7. Fix or ticket every stall before going on. A dry run on this machine
   without the simulator is logged in the WP9 notes; it is not a substitute.

## B. Decisions still open

Collected from the preparation run. None blocks the mechanics below, but
several change what users see on day one.

* **Default screenshot output.** `sim shot`/`shots` produce the *framed*
  render (Garmin's device image) unless `--native`. Make native the default
  for the public release, so a user never commits Garmin art by accident?
* **Rectangular devices.** venusq2 shots are square crops (about 40 px clipped
  top and bottom); face detection assumes round. Fix before 0.1 or list as a
  known limit in the README?
* **Committed baselines.** Even `--native` crops keep the bezel ring and button
  labels from the device art, so no example shots are committed and `init
  --ci` expects users to commit their own under `baselines/`. A display-only
  crop would make baselines safe to commit. OK as is for 0.1?
* **Names.** PyPI `ciqkit` and npm `ciqkit-sdk-mcp` (check availability, see
  D); repo owner `JohannesHoh`; the MCP server key `garmin-sdk` in generated
  `.mcp.json` files.
* **npm auth.** `NPM_TOKEN` secret (as written) or npm trusted publishing.
* **Partial releases.** PyPI and npm jobs are independent, so one can publish
  while the other fails. Acceptable, or chain npm after PyPI?
* **Smaller items:** CHANGELOG still says "Extracted from a private
  toolchain"; built-in build names the output after the project directory;
  no `CIQKIT_DEVICE`/`CIQKIT_TEST_BUILD_CMD` env vars; a malformed
  `ciqkit.toml` stops `doctor --project` early; no `ciqkit build` (compile
  only) command; the tutorial installs `ciqkit[sim,ocr]`;
  `src/ciqkit/redact.py` has had no caller since the store package was
  removed (keep as a utility or drop); gotchas 008/009 are about climbing
  sport types and say so; one socket test
  (`test_connections_beyond_the_cap…`) failed once and passed on rerun.
* **Input confirmation.** `act` diffs the whole simulator window, so a ticking
  clock or animation in the app, or a notification banner over the window,
  can confirm a gesture that did not land. Documented in docs/sim.md;
  anchors are the guard. Revisit if it bites.

## C. GitHub settings

On the **public** repository (see E for which one that is):

* **About**: description
  `Unofficial dev tooling for Garmin Connect IQ: a scheduling broker for the simulator, verified screenshots, layout checks, an SDK lookup MCP server.`
  Website: the README or the tutorial.
  Topics: `garmin`, `connect-iq`, `connectiq`, `monkey-c`, `smartwatch`,
  `watchface`, `simulator`, `screenshot-testing`, `mcp`,
  `model-context-protocol`, `claude-code`, `developer-tools`.
* **Security → Private vulnerability reporting**: enable (SECURITY.md and the
  issue template link to it).
* **Security → Dependabot alerts** and **Dependabot security updates**: enable
  (`.github/dependabot.yml` already covers pip, npm and Actions).
* **Secret scanning** and **push protection**: enable (free on public repos).
* **Features → Discussions**: enable; the issue template's contact links can
  then point questions there.
* **Branches → branch protection (or a ruleset) on `main`**: require a pull
  request, require status checks to pass, block force pushes and deletion.
  Required checks (job names from `ci.yml`; they appear once CI has run on
  the repo):
  `python 3.11 (ubuntu-latest)`, `python 3.13 (ubuntu-latest)`,
  `python 3.11 (macos-latest)`, `python 3.13 (macos-latest)`,
  `sdk-mcp (node 18)`, `sdk-mcp (node 22)`,
  `no redistributed Garmin material`.
* **Actions → General**: workflow permissions "Read repository contents"
  (the workflows ask for more where they need it).
* **Environments → `pypi`**: create it; add yourself as required reviewer so a
  tag push waits for a click before publishing.

## D. Registries

### PyPI (trusted publishing, no token)

1. Check the name: `pip index versions ciqkit` (or pypi.org/project/ciqkit)
   should find nothing.
2. pypi.org → Your account → Publishing → **Add a new pending publisher**:
   * PyPI project name: `ciqkit`
   * Owner: `JohannesHoh`
   * Repository name: `ciqkit`
   * Workflow name: `release.yml`
   * Environment name: `pypi`
3. Optional first: do the same on test.pypi.org and point a copy of the
   workflow at it, if you want a rehearsal.

### npm

1. Check the name: `npm view ciqkit-sdk-mcp` should 404.
2. Either
   * **token** (what `release.yml` does now): npmjs.com → Access Tokens →
     granular token, publish rights, limited to `ciqkit-sdk-mcp` (for the
     very first publish, a token that may create packages), saved as the
     repository secret `NPM_TOKEN`; or
   * **trusted publishing**: after the package exists, configure GitHub
     Actions as trusted publisher on npmjs.com (repo, `release.yml`), then
     remove `NODE_AUTH_TOKEN` from the npm job and the secret.
3. `--provenance` is already on; it needs the public repo and `id-token:
   write`, both in place.
4. Enable 2FA for publishing on the npm account.

## E. History: which branch becomes the public `main`

Two candidates.

| | `public-squash` (orphan, one commit) | `git filter-repo` on full history |
|---|---|---|
| What is public | the final tree only | every commit, rewritten |
| Risk | none from history; nothing to audit but one tree | every past commit must be clean: the removed `ciqkit.store` (dashboard automation), private paths, the real Garmin loading-screen PNG, private app names. One missed blob is public forever |
| Effort | done | write path and text filters, re-audit every commit |
| Loses | `git blame` / bisect for the pre-release history | nothing |
| Authors | one commit, GitHub noreply address | rewrite needed: most commits carry a personal email |

**Recommendation: `public-squash`.** The history's value (blame, bisect) is
mostly for you, and you keep it in the private repo. Its cost if one filter
is wrong is permanent.

**Never make the existing private repository public**: it holds the full
history, and also branches, closed PRs and their refs, which a visibility
switch exposes too. Publish into a **new, empty** repository instead:

```bash
# 0. Audit what is about to go out (one commit, noreply author, clean tree).
git log --format='%H %an <%ae> | %cn <%ce>' public-squash
git ls-tree -r --name-only public-squash | grep -Ei '\.(prg|iq|der|pem|jks)$|sdk-index' && echo STOP
# Private strings (your email, home path, private repo and app names) live in a
# file OUTSIDE the repo, one per line, so this checklist does not publish them.
git grep -n -i -F -f ~/.ciqkit-private-patterns public-squash && echo STOP

# 1. On GitHub: rename the private repo, e.g. JohannesHoh/ciqkit -> ciqkit-private
#    (Settings -> General -> Repository name). Keep it private.
# 2. On GitHub: create a NEW repository JohannesHoh/ciqkit, public, completely
#    empty (no README, licence or .gitignore). Creating it under the old name
#    also removes the rename redirect.
# 3. Locally: point origin at the private one, add the public one, push ONE
#    branch under the name main. No --all, no --mirror, no --tags.
git remote set-url origin git@github.com:JohannesHoh/ciqkit-private.git
git remote add public git@github.com:JohannesHoh/ciqkit.git
git push public public-squash:main

# 4. Check the result: exactly one commit, one branch, no tags.
git ls-remote public
```

From then on, develop against `public` (fresh clone of the public repo is the
simplest). Do not merge the private history into it, and do not push the
private `main` to it: either would publish everything step 3 avoided.

If you choose `filter-repo` after all: work in a fresh `git clone --mirror`,
use `--invert-paths` for `src/ciqkit/store`, `tests/test_store.py`,
`tests/data/ciq_loading_screen.png` and any other removed private files,
`--replace-text` for private strings, `--mailmap` for the authors, then repeat
the audit in step 0 against **every** commit
(`git log -p --all | grep -i -F -f ~/.ciqkit-private-patterns`),
and push into a new empty repository exactly as above.

## F. Cut the release

1. Settle the decisions in B that affect the release.
2. Versions: `src/ciqkit/__init__.py` (`__version__`) and
   `tools/sdk-mcp/package.json` (`version`; also run `npm install` so
   `package-lock.json` agrees) must both equal the tag without the `v`.
   For 0.1.0 they already do.
3. CHANGELOG: change `## [0.1.0] — unreleased` to today's date, and drop the
   "Not published yet" paragraph.
4. Merge to `main` through a PR so the required checks run.
5. Tag and push the tag:
   ```bash
   git switch main && git pull
   git tag -a v0.1.0 -m "ciqkit 0.1.0"
   git push public v0.1.0
   ```
6. Watch **Actions → Release**: `ci` must pass, then approve the `pypi`
   environment. The PyPI job checks the wheel/sdist names against the tag and
   that no build output, key or SDK index is inside; the npm job checks the
   package version and that no `sdk-index.json` is packed.
7. If one registry job fails: fix and re-run only that job. Neither registry
   accepts the same version twice, so a fix that needs new code means a new
   version (0.1.1), not a re-tag.
8. Verify from a clean venv: `pip install "ciqkit[sim]"`, `ciqkit --version`;
   `npx -y ciqkit-sdk-mcp` starts; then create the GitHub release from the
   tag with the CHANGELOG section as notes.

## G. After the release

* `examples/tictactoe/.mcp.json`: switch `garmin-sdk` from the repo-relative
  `run-server.sh` to `{"command": "npx", "args": ["-y", "ciqkit-sdk-mcp"]}`
  (what `ciqkit new` and `ciqkit init` already write), and drop the
  `.mcp.json` exception in `tests/test_wp6_new_init.py`.
* docs/sdk-mcp.md, docs/tutorial/04 and tools/sdk-mcp/README.md: remove the
  "not published to npm yet" notes; make npx route A the default.
* README: badges for CI (`actions/workflows/ci.yml/badge.svg`), PyPI version,
  Python versions, npm version, licence; install line `pip install
  "ciqkit[sim]"` without the clone caveat.
* CHANGELOG: open a new `## [Unreleased]` section.
* Pin the release announcement or a "start here" Discussion that links the
  tutorial.
