"""Hostile wire/MCP job fields are refused by validate_job before anything runs."""
import math
import os

import pytest

from ciqkit.sim import protocol as proto
from ciqkit.sim import storage
from ciqkit.sim.protocol import Chunk, ChunkKind, Job

ABS = os.path.abspath(os.sep + "out")


def _errs(chunk=None, **job):
    chunks = [chunk] if chunk is not None else [Chunk(ChunkKind.LOAD, {"prg": "/x.prg"})]
    return proto.validate_job(Job(app="a", chunks=chunks, **job))


@pytest.mark.parametrize("name", ["../../x", "/etc/x", "a/b", "a\\b", "..", ".", "C:x",
                                  "a\0b", "x" * 200])
@pytest.mark.parametrize("kind", [ChunkKind.SHOT, ChunkKind.RECORD])
def test_name_must_be_a_plain_stem(kind, name):
    assert any("plain file name" in e for e in _errs(Chunk(kind, {"name": name})))


@pytest.mark.parametrize("name", ["home", "screen-1", "fenix7_main.v2"])
def test_plain_names_pass(name):
    assert _errs(Chunk(ChunkKind.SHOT, {"name": name, "out_dir": ABS})) == []


def test_shot_on_step_name_and_out_dir_checked():
    step = {"kind": "shot_on", "marker": "m", "name": "../x", "out_dir": "rel"}
    errs = proto.step_errors(step)
    assert any("plain file name" in e for e in errs) and any("out_dir" in e for e in errs)


@pytest.mark.parametrize("od", ["rel/dir", "../x", 7, ["/a"], "/a\0b"])
def test_out_dir_must_be_absolute_string(od):
    assert any("out_dir" in e for e in _errs(Chunk(ChunkKind.SHOT, {"name": "a", "out_dir": od})))


def test_out_dir_unset_is_fine():
    assert _errs(Chunk(ChunkKind.SHOT, {"name": "a", "out_dir": None})) == []


def test_front_ends_resolve_relative_out_dir():
    from ciqkit.sim.jobs import abs_out_dir
    assert os.path.isabs(abs_out_dir("shots"))
    assert abs_out_dir(None) is None


@pytest.mark.parametrize("dev", ["x --flag y", "-d", "--help", "a;b", "a b", "$(x)", "d" * 65])
def test_device_injection_refused(dev):
    assert any("device" in e for e in _errs(device=dev))


@pytest.mark.parametrize("dev", ["venu3", "fenix7", "fr965", "epix2pro47mm",
                                 "approachs70_47mm", "fakewatch", ""])
def test_real_device_ids_pass(dev):
    assert _errs(device=dev) == []


@pytest.mark.parametrize("key", ['"', "'", "\\", "ab", "enter; do shell script", "\n", "é"])
def test_hostile_key_refused(key):
    assert any("key" in e for e in proto.step_errors({"kind": "key", "key": key}))


@pytest.mark.parametrize("key", ["up", "enter", "esc", "space", "a", "7", "/"])
def test_legit_key_passes(key):
    assert proto.step_errors({"kind": "key", "key": key}) == []


def test_modifiers_must_be_known_names():
    assert proto.step_errors({"kind": "key", "key": "a", "modifiers": ["cmd", "shift"]}) == []
    for bad in (["x"], "cmd", [1]):
        assert proto.step_errors({"kind": "key", "key": "a", "modifiers": bad})


@pytest.mark.parametrize("v", [math.nan, math.inf, -1, "5", True, None])
def test_wait_timeout_refused(v):
    assert proto.wait_timeout_error(v) is not None


def test_wait_timeout_ok():
    assert proto.wait_timeout_error(0) is None and proto.wait_timeout_error(600) is None


def test_nan_chunk_budget_refused():
    c = Chunk(ChunkKind.LOAD, {"prg": "/x.prg"}, max_s=math.nan)
    assert _errs(c)


def test_chunk_and_step_caps():
    many = [Chunk(ChunkKind.LOAD, {"prg": "/x.prg"})] * (proto.MAX_CHUNKS + 1)
    assert any("chunks" in e for e in proto.validate_job(Job(app="a", chunks=many)))
    steps = [{"kind": "tap"}] * (proto.MAX_STEPS + 1)
    assert any("steps" in e for e in _errs(Chunk(ChunkKind.REACH, {"steps": steps})))
    ok = [{"kind": "tap"}] * proto.MAX_STEPS
    assert not any("steps" in e for e in _errs(Chunk(ChunkKind.REACH, {"steps": ok})))


def test_long_or_nul_strings_refused():
    for v in ("p" * 5000, "a\0b"):
        assert _errs(Chunk(ChunkKind.LOAD, {"prg": v}))


@pytest.mark.parametrize("p", ["x/..", "..", ".", "/a/b/.."])
def test_storage_name_refuses_dot_names(p):
    with pytest.raises(ValueError):
        storage.storage_name(p)


@pytest.mark.parametrize("dev", ["-x; touch /tmp/pwn", "a b", "-d", 7])
@pytest.mark.parametrize("kind", [ChunkKind.LOAD, ChunkKind.SHOT, ChunkKind.ACT])
def test_chunk_device_override_is_validated_too(kind, dev):
    args = {"prg": "/x.prg", "name": "a", "button": "enter", "device": dev}
    assert any("device" in e for e in _errs(Chunk(kind, args), device="venu3"))


def test_a_device_with_a_trailing_newline_is_refused():
    assert proto.device_error("venu3\n")
    assert proto.device_error("venu3") is None


def test_a_relative_shot_on_out_dir_is_made_absolute_by_the_builder(tmp_path, monkeypatch):
    from ciqkit.sim import jobs
    monkeypatch.chdir(tmp_path)
    c = jobs.reach_chunk(screen="s", steps=[
        {"kind": "shot_on", "marker": "X", "name": "p1", "out_dir": "shots"},
        {"kind": "key", "key": "up"}])
    st = c.args["steps"][0]
    assert os.path.isabs(st["out_dir"])
    assert os.path.realpath(st["out_dir"]) == os.path.realpath(str(tmp_path / "shots"))
    assert c.args["steps"][1] == {"kind": "key", "key": "up"}
