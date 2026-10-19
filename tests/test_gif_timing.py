"""GIF playback timing: slow simulator grabs must still animate."""
import pytest

from ciqkit.gifretime import retime
from ciqkit.sim.session import _compress_durations, _flatten_overhead_gaps


def test_slow_capture_is_compressed_to_the_requested_median():
    # ~2.1 s per grab: what a DEGRADED capture looks like, and what every
    # grab cost before the capture-rate fix -> unwatchable at wall-clock.
    wall = [2220, 2120, 2160, 2110, 2140]
    out, speedup = _compress_durations(wall, 120)
    assert max(out) <= 200
    assert speedup == pytest.approx(2140 / 120, rel=0.01)


def test_ratios_survive_compression():
    wall = [2000, 2000, 6000, 2000]     # one real stall, 3x the others
    out, _ = _compress_durations(wall, 100)
    assert out[2] == pytest.approx(3 * out[0], rel=0.05)


def test_outliers_are_clamped_into_a_watchable_band():
    wall = [2000, 2000, 120000, 2000]   # a 2-minute stall
    out, _ = _compress_durations(wall, 100)
    assert min(out) >= 20 and max(out) <= 1000


def test_a_clip_that_already_plays_fast_is_left_alone():
    wall = [80, 90, 85]
    out, speedup = _compress_durations(wall, 120)
    assert out == wall and speedup == 1.0


def test_disabled_returns_wall_clock():
    wall = [2220, 2120]
    assert _compress_durations(wall, 0) == (wall, 1.0)


def test_retime_rewrites_an_existing_slow_gif(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    frames = [Image.new("RGB", (8, 8), c) for c in ("red", "green", "blue")]
    p = tmp_path / "slow.gif"
    frames[0].save(p, save_all=True, append_images=frames[1:],
                   duration=[2100, 2100, 2100], loop=0)

    r = retime(str(p), 120)
    assert r["speedup"] > 10

    with Image.open(p) as im:
        assert im.n_frames == 3
        durs = []
        for i in range(im.n_frames):
            im.seek(i)
            durs.append(im.info["duration"])
    assert max(durs) <= 200


def test_the_default_pace_leaves_each_sampled_pose_readable():
    """When grabs are ~2.1 s apart, consecutive frames are distinct POSES.
    At the default pace every pose must hold long enough to register as a step
    (>= ~0.3 s) without freezing (<= 1 s) -- the old 120 ms default flashed 16
    poses past in under 2 s and the clip read as unwatchable flicker."""
    from ciqkit.sim.protocol import DEFAULT_PLAYBACK_MS
    wall = [2220, 2120, 2160, 2110, 2140, 2180, 2100]
    out, _ = _compress_durations(wall, DEFAULT_PLAYBACK_MS)
    assert all(300 <= d <= 1000 for d in out)
    assert sorted(out)[len(out) // 2] == pytest.approx(DEFAULT_PLAYBACK_MS,
                                                       rel=0.05)


def test_every_surface_agrees_on_the_default_playback_pace():
    """The default lives in protocol.py; the CLI flag, the MCP schema and
    SimSession.record must all quote it, or an agent reading one surface gets
    a different clip than another."""
    import inspect

    from ciqkit.sim import mcp_server
    from ciqkit.sim.client import build_parser
    from ciqkit.sim.protocol import DEFAULT_PLAYBACK_MS
    from ciqkit.sim.session import SimSession

    sig = inspect.signature(SimSession.record)
    assert sig.parameters["playback_ms"].default == DEFAULT_PLAYBACK_MS
    assert SimSession.DEFAULT_PLAYBACK_MS == DEFAULT_PLAYBACK_MS

    args = build_parser().parse_args(["record", "clip"])
    assert args.playback_ms == DEFAULT_PLAYBACK_MS

    rec = next(t for t in mcp_server.TOOLS if t["name"] == "ciqsim_record")
    prop = rec["inputSchema"]["properties"]["playback_ms"]
    assert prop["default"] == DEFAULT_PLAYBACK_MS
    assert str(DEFAULT_PLAYBACK_MS) in prop["description"]


# ── ciqkit's own bookkeeping must not show up as a pause in the clip ─────────
# Honest wall-clock durations are the right default -- a real stall on the
# watch should read as a stall. But two gaps per clip measure US, not the app:
# the canvas-mask build after frame 0 and the window re-raise before every
# refocus frame. Measured on a real clip they were 720 ms and 1450 ms against a
# 170 ms median: an 8x freeze every ~5 s of playback.

def test_an_overhead_gap_is_flattened_to_the_honest_median():
    wall = [720, 170, 180, 170, 1450, 170, 180]
    out = _flatten_overhead_gaps(wall, {0, 4})
    assert out[0] == 170 and out[4] == 170      # median of the honest five
    assert [out[i] for i in (1, 2, 3, 5, 6)] == [170, 180, 170, 170, 180]


def test_a_real_stall_is_left_alone():
    """Only the gaps record() FLAGS are touched; a slow redraw elsewhere in the
    clip still reads as the pause it was."""
    wall = [170, 180, 6000, 170, 180]
    assert _flatten_overhead_gaps(wall, {0}) == [170, 180, 6000, 170, 180]


def test_an_overhead_gap_that_cost_nothing_is_not_stretched():
    """Flattening must only ever SHORTEN. Padding a cheap gap out to the median
    would invent a pause that never happened."""
    wall = [20, 400, 380, 420]
    assert _flatten_overhead_gaps(wall, {0})[0] == 20


def test_nothing_to_compare_against_is_a_no_op():
    """All-overhead (a 1-2 frame clip) has no honest median to borrow."""
    assert _flatten_overhead_gaps([500, 600], {0, 1}) == [500, 600]
    assert _flatten_overhead_gaps([], {0}) == []


def test_flattening_runs_before_compression_so_a_slow_clip_is_unharmed():
    """A degraded clip still gets compressed; flattening just removes our own
    spikes from the ratios first."""
    wall = [2100, 2050, 3400, 2100]          # gap 2 = a refocus on a slow clip
    flat = _flatten_overhead_gaps(wall, {2})
    out, speedup = _compress_durations(flat, 500)
    assert flat[2] == 2100                   # median of the honest three
    assert max(out) <= 1000 and speedup > 1.0
