"""Atheris harness: the simulator test harness's stdout parser.

    python fuzz/fuzz_test_output.py -max_total_time=60

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
        targets.check_test_output(
            targets.build_test_output(lambda n: fdp.ConsumeIntInRange(0, n - 1)))
    else:
        targets.check_test_output_bytes(fdp.ConsumeBytes(fdp.remaining_bytes()))


if __name__ == "__main__":
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
