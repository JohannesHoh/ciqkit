"""Atheris harness: control-channel SUBMIT lines.

    python fuzz/fuzz_wire.py -max_total_time=60 fuzz/corpus/wire

Linux x86_64 only (Atheris wheels). The properties live in targets.py and also
run in pytest via tests/test_fuzz_targets.py.
"""

import sys

import atheris

with atheris.instrument_imports():
    import targets


def TestOneInput(data: bytes) -> None:  # noqa: N802 -- libFuzzer's name
    fdp = atheris.FuzzedDataProvider(data)
    if fdp.ConsumeBool():
        # Structured: a job assembled from the schema's vocabulary.
        targets.check_submit(targets.build_job(lambda n: fdp.ConsumeIntInRange(0, n - 1)))
    else:
        # Raw: whatever bytes reach the connection handler.
        targets.check_wire_line(fdp.ConsumeBytes(fdp.remaining_bytes()))


if __name__ == "__main__":
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
