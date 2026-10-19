# Contributing

Thanks for looking. This is a small project maintained by one person; please
set expectations accordingly — reviews may take a week or two.

## The most useful contribution

**A bug report with a reproducible case.** ciqkit is developed on one macOS
machine against a handful of projects. Almost every rough edge you hit will be
one nobody has seen.

A good report includes:

* the output of `ciqkit doctor`;
* the exact command you ran and the JSON it printed;
* what you expected instead;
* for capture problems, the PNG that came out.

## Before you start on a feature

Read [docs/scope.md](docs/scope.md). Two categories are declined regardless of
quality:

* automation of any Garmin online service (developer dashboard, Garmin Connect,
  crash-report backend, or any other authenticated Garmin property);
* anything that redistributes Garmin material — SDK files, documentation text,
  device images, sample code.

For anything else non-trivial, open an issue first so you do not spend an
evening on something that turns out to be out of scope.

## Development setup

```bash
git clone https://github.com/JohannesHoh/ciqkit
cd ciqkit
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,sim]"
```

Checks, all of which CI runs:

```bash
pytest                 # unit tests: no simulator, no SDK, no GUI needed
ruff check src tests   # lint
ciqkit doctor          # your own environment (not run in CI)
```

For the SDK MCP server:

```bash
cd tools/sdk-mcp
npm install
npm run build          # must typecheck clean
npm run parse-sdk      # needs a Connect IQ SDK installed
```

## Tests

Unit tests must run **without a simulator, an SDK, or a display**, so CI can run
them on Linux. `tests/test_scheduler.py` shows the pattern: a `FakeSession`
records the order chunks execute in, which is enough to prove priority ordering,
preemption, resume and bump.

If your change touches the scheduler, add a case there. If it touches image
processing, add a small synthetic image rather than committing a real
screenshot.

## Code style

* `ruff check src tests` must pass; line length 100.
* Comments should explain *why*, especially where the code looks odd — most of
  the odd-looking code here is working around a real simulator behaviour, and
  the next person needs to know which.
* Public functions get a docstring saying what they do and what they assume.
* Keep `ciqkit.sim.protocol` dependency-free and stdlib-only.

## Adding a gotcha

`tools/sdk-mcp/data/gotchas.json` entries need evidence — something that
actually broke, not something that looks wrong. Include:

```jsonc
{
  "id": "gotcha-0NN",
  "title": "Short, searchable",
  "api": "Graphics",              // module or 'Language'
  "tags": ["drawing"],
  "severity": "high",             // low | medium | high | critical
  "wrong": "What people do.",
  "correct": "What to do instead, and why.",
  "codeWrong": "one or two lines",
  "codeCorrect": "one or two lines",
  "source": "how you know — a forum thread, an SDK doc, field use"
}
```

Please do not include project names, app IDs, or anything else identifying.

## Maintainers

Releasing (GitHub settings, PyPI/npm publishing, tagging) is described in
[docs/release-checklist.md](docs/release-checklist.md).

## Security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).

## Licence

By contributing you agree your contribution is licensed under the
[Apache License 2.0](LICENSE), per section 5 of that licence. There is no CLA.
