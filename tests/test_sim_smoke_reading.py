"""scripts/sim_smoke_reading.py against a synthetic docs/sim-smoke.jsonl.

No simulator: the reading is pure arithmetic over the jsonl lines.
"""
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rd = _load("sim_smoke_reading")
OPS = rd.sim_smoke.OPS
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def _write(path, rows):
    path.write_bytes("".join(json.dumps(r) + "\n" for r in rows).encode("utf-8"))


def _row(op, ok=True, platform="macos", days_ago=1):
    at = (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")
    return {"op": op, "ok": ok, "detail": "", "platform": platform, "device": "venu3",
            "sdk": "9.1.0", "sha": "abc1234", "at": at}


def test_all_ops_passing_reads_one(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [_row(op) for op in OPS])
    assert rd.reading(str(p), "macos", NOW) == 1.0


def test_failed_skipped_stale_and_other_platform_lines_do_not_count(tmp_path):
    p = tmp_path / "s.jsonl"
    rows = [_row(OPS[0]),                              # counts
            _row(OPS[1], ok=False),                    # failed
            _row(OPS[2], days_ago=31),                 # outside the window
            _row(OPS[3], platform="windows"),          # another platform
            _row("not_an_op"),                         # not in the op list
            _row(OPS[0])]                              # duplicate pass, counted once
    _write(p, rows)
    assert rd.reading(str(p), "macos", NOW) == pytest.approx(1 / len(OPS))
    assert rd.reading(str(p), "windows", NOW) == pytest.approx(1 / len(OPS))


def test_a_later_failure_does_not_erase_a_pass_in_the_window(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [_row(OPS[0], days_ago=5), _row(OPS[0], ok=False, days_ago=1)])
    assert rd.reading(str(p), "macos", NOW) == pytest.approx(1 / len(OPS))


def test_missing_or_empty_file_is_no_reading(tmp_path):
    with pytest.raises(rd.NoReading):
        rd.reading(str(tmp_path / "absent.jsonl"), "macos", NOW)
    p = tmp_path / "empty.jsonl"
    p.write_bytes(b"\n{torn line\n")
    with pytest.raises(rd.NoReading):
        rd.reading(str(p), "macos", NOW)


def test_cli_prints_value_or_nothing(tmp_path):
    # The script reads sim_smoke.LOG; point a copy of both scripts at tmp_path.
    (tmp_path / "docs").mkdir()
    sub = tmp_path / "scripts"
    sub.mkdir()
    for name in ("sim_smoke", "sim_smoke_reading"):
        with open(os.path.join(SCRIPTS, name + ".py"), "rb") as fh:
            (sub / (name + ".py")).write_bytes(fh.read())
    script = str(sub / "sim_smoke_reading.py")

    r = subprocess.run([sys.executable, script], capture_output=True, text=True)
    assert r.returncode != 0 and r.stdout == "" and "does not exist" in r.stderr

    _write(tmp_path / "docs" / "sim-smoke.jsonl",
           [{**_row(OPS[0]), "platform": rd.sim_smoke.platform_name(),
             "at": datetime.now(UTC).isoformat(timespec="seconds")}])
    r = subprocess.run([sys.executable, script], capture_output=True, text=True)
    assert r.returncode == 0
    assert json.loads(r.stdout) == {"value": round(1 / len(OPS), 4)}
