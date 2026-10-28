#!/usr/bin/env python3
"""The simOpsVerified reading: share of sim_smoke.OPS verified on a real simulator.

Reads docs/sim-smoke.jsonl and prints {"value": n}, n in 0..1: the share of the
ops in scripts/sim_smoke.py that have a passing line in the last 30 days on the
platform this runs on. A skipped or failed op counts as unverified.

No file, or a file with no usable lines, is not a reading of 0 -- it is no
reading: nothing goes to stdout, the reason goes to stderr, and the exit code
is non-zero. Stdlib only; runs under a bare python3.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sim_smoke  # noqa: E402

WINDOW = timedelta(days=30)


class NoReading(Exception):
    pass


def _when(s: str) -> datetime:
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def reading(path: str, platform: str, now: datetime | None = None) -> float:
    if not os.path.isfile(path):
        raise NoReading(f"{path} does not exist; run scripts/sim_smoke.py first")
    rows = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
                row["_at"] = _when(row["at"])
            except (ValueError, KeyError, TypeError, AttributeError):
                continue   # a torn or hand-mangled line is not a pass
            rows.append(row)
    if not rows:
        raise NoReading(f"{path} has no smoke lines; run scripts/sim_smoke.py first")
    since = (now or datetime.now(UTC)) - WINDOW
    verified = {r.get("op") for r in rows
                if r.get("ok") is True and r.get("platform") == platform
                and r["_at"] >= since}
    return sum(op in verified for op in sim_smoke.OPS) / len(sim_smoke.OPS)


def main() -> int:
    try:
        n = reading(sim_smoke.LOG, sim_smoke.platform_name())
    except NoReading as exc:
        print(f"sim_smoke_reading: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"value": round(n, 4)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
