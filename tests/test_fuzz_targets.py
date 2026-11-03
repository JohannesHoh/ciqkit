"""The fuzz/ properties, replayed with a fixed seed and capped runs.

Atheris only ships Linux x86_64 wheels, so the coverage-guided runs happen in
CI's fuzz job. This keeps the same properties in every platform's pytest.
"""

import os
import random
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "fuzz"))
import targets  # noqa: E402

RUNS = 3000


def _pick(rng):
    return lambda n: rng.randrange(n)


def test_submit_properties_hold_for_generated_jobs():
    rng = random.Random(20261102)
    for _ in range(RUNS):
        targets.check_submit(targets.build_job(_pick(rng)))


def test_wire_lines_survive_random_bytes():
    rng = random.Random(1)
    for _ in range(RUNS):
        targets.check_wire_line(rng.randbytes(rng.randrange(64)))
    targets.check_wire_line(b"[" * 100_000)          # json recursion limit


def test_test_output_properties_hold_for_generated_text():
    rng = random.Random(20261102)
    for _ in range(RUNS):
        targets.check_test_output(targets.build_test_output(_pick(rng)))


@pytest.mark.parametrize("text", [
    "Passed 1 " * 50_000,                          # was quadratic: ~minutes
    "Passed: " + "9" * 5000 + " Failed: 0",        # int() 4300-digit limit
    "PASSED (passed=" + "1" * 5000 + ", failed=0, errors=0)",
    "Ran " + "7" * 5000 + " tests",
])
def test_test_output_pathological_inputs(text):
    t0 = time.perf_counter()
    targets.check_test_output(text)
    assert time.perf_counter() - t0 < targets.PARSE_BUDGET_S


@pytest.mark.parametrize("job,reason", [
    ({"app": "a", "id": 7, "chunks": [{"kind": "ensure"}]}, "id must be a string"),
    ({"app": {"x": 1}, "chunks": [{"kind": "ensure"}]}, "app must be a string"),
    ({"app": "a", "chunks": [{"kind": ["shot"]}]}, "unknown chunk kind"),
    ({"app": "a", "chunks": [{"kind": "shot", "args": {"name": ["x"]}}]}, "missing name"),
    ({"app": "a", "chunks": [{"kind": "act", "args": {"kind": "swipe", "edge": []}}]},
     "swipe edge"),
])
def test_wrongly_typed_submissions_are_refused_with_a_reason(job, reason):
    from ciqkit.sim.protocol import Job, validate_job
    errs = validate_job(Job.from_dict(job))
    assert any(reason in e for e in errs), errs
