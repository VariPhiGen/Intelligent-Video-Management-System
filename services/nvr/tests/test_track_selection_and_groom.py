"""Two NVR decisions that silently return the wrong footage.

Track selection decides WHICH recording answers a playback request, and
grooming decides which footage gets rewritten keyframe-only. Both used to fail
by producing a valid-looking result: a playlist that is short, or a scrub track
that plays as a slideshow. Neither raises, neither logs, and both look to an
operator like the recorder simply missed something.

Run (from services/nvr): python -m pytest tests -q
"""
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import hls  # noqa: E402
from storage.grooming import GroomingManager  # noqa: E402


@dataclass
class Seg:
    start_epoch: float
    duration: float


# ── covered_seconds ──────────────────────────────────────────────────────────

def test_contiguous_segments_sum():
    segs = [Seg(0, 60), Seg(60, 60), Seg(120, 60)]
    assert hls.covered_seconds(segs, 0, 180) == 180


def test_segments_are_clipped_to_the_window():
    """A segment straddling the boundary counts only its inside part —
    otherwise a single segment could 'cover' more than the window itself."""
    assert hls.covered_seconds([Seg(-30, 60)], 0, 100) == 30
    assert hls.covered_seconds([Seg(80, 60)], 0, 100) == 20


def test_overlapping_segments_count_once():
    """Two tracks' worth of the same minute is still one minute of footage."""
    assert hls.covered_seconds([Seg(0, 60), Seg(30, 60)], 0, 100) == 90


def test_a_gap_is_not_covered():
    segs = [Seg(0, 60), Seg(120, 60)]
    assert hls.covered_seconds(segs, 0, 180) == 120


def test_segments_wholly_outside_the_window_contribute_nothing():
    assert hls.covered_seconds([Seg(500, 60)], 0, 100) == 0


def test_empty_and_degenerate_inputs():
    assert hls.covered_seconds([], 0, 100) == 0
    assert hls.covered_seconds([Seg(0, 60)], 100, 100) == 0


def test_unsorted_input_is_handled():
    segs = [Seg(120, 60), Seg(0, 60), Seg(60, 60)]
    assert hls.covered_seconds(segs, 0, 180) == 180


# ── The selection rule the coverage measurement exists for ──────────────────
#
# server.py ranks by coverage first and cost second. These pin that rule against
# the shapes that made the old cost-only version return truncated playlists.

def _pick(tracks, from_epoch, to_epoch):
    """Name of the track hls_playlist would serve, over (name, rank, segments).

    Calls the real rule (hls.select_track) rather than restating it — a test
    that reimplements the decision it is guarding passes happily while the
    shipped code does something else."""
    cands = [(rank, hls.covered_seconds(segs, from_epoch, to_epoch), name)
             for name, rank, segs in tracks if segs]
    return hls.select_track(cands)[2]


CHEAP = hls.rank_track("ts", 704, 576)          # sub: stream-copy, small
DEAR = hls.rank_track("h264", 1920, 1080)       # main: per-segment transcode


def test_a_partly_covering_sub_loses_to_a_fully_covering_main():
    """The reported bug: a sub enabled at 14:00 won a 13:00-15:00 request
    because it was cheaper and had *a* segment in the window, and the main's
    complete first hour was served as a recording gap."""
    window = (13 * 3600, 15 * 3600)
    sub = [Seg(14 * 3600 + i * 60, 60) for i in range(60)]      # 14:00-15:00
    main = [Seg(13 * 3600 + i * 60, 60) for i in range(120)]    # 13:00-15:00
    assert _pick([("cam_sub", CHEAP, sub), ("cam", DEAR, main)], *window) == "cam"


def test_the_cheap_track_still_wins_when_both_cover_the_window():
    """The optimisation this replaced must survive: equal coverage, cost decides."""
    window = (0, 3600)
    segs = [Seg(i * 60, 60) for i in range(60)]
    assert _pick([("cam_sub", CHEAP, list(segs)),
                  ("cam", DEAR, list(segs))], *window) == "cam_sub"


def test_a_second_of_boundary_drift_does_not_lose_the_sub():
    """The two tracks are separate ffmpeg processes, so their segment
    boundaries never line up exactly. Demanding identical coverage would reject
    the sub on rounding and quietly disable the optimisation for everyone."""
    window = (0, 3600)
    main = [Seg(i * 60, 60) for i in range(60)]
    sub = [Seg(0.5 + i * 60, 60) for i in range(60)]            # half a second late
    assert _pick([("cam_sub", CHEAP, sub), ("cam", DEAR, main)], *window) == "cam_sub"


def test_a_sub_covering_materially_less_loses_even_when_it_is_close():
    """Beyond the tolerance, footage wins over cost — 5 minutes missing from a
    one-hour ask is a real hole, not drift."""
    window = (0, 3600)
    main = [Seg(i * 60, 60) for i in range(60)]
    sub = [Seg(i * 60, 60) for i in range(55)]                  # 5 minutes short
    assert _pick([("cam_sub", CHEAP, sub), ("cam", DEAR, main)], *window) == "cam"


def test_the_only_track_with_footage_is_used_however_thin():
    """Coverage ranks candidates; it never rejects the last one. A thin
    playlist beats a 404 when it is all the footage there is."""
    window = (0, 3600)
    sub = [Seg(3000, 60)]
    assert _pick([("cam_sub", CHEAP, sub), ("cam", DEAR, [])], *window) == "cam_sub"


# ── Grooming: 0 means never, and has to survive the provider ────────────────

def _groomer(tmp_path, default_days, provider):
    class _Idx:
        def get_cameras(self):
            return ["cam", "cam_sub"]
    return GroomingManager(_Idx(), groom_after_days=default_days,
                           cameras_config=None, groom_map_provider=provider)


def test_a_zero_from_the_provider_means_never_groom(tmp_path):
    """The bug: the provider's 0 was dropped by a truthy test, so the sub fell
    back to the appliance default and was rewritten keyframe-only — turning the
    scrub track into a slideshow, the one thing it exists to prevent."""
    g = _groomer(tmp_path, 30, lambda: {"cam_sub": 0})
    assert g._camera_groom_map()["cam_sub"] == 0


def test_the_main_still_inherits_the_appliance_default(tmp_path):
    g = _groomer(tmp_path, 30, lambda: {"cam_sub": 0})
    assert g._camera_groom_map()["cam"] == 30


def test_a_positive_override_still_wins(tmp_path):
    g = _groomer(tmp_path, 30, lambda: {"cam": 7})
    assert g._camera_groom_map()["cam"] == 7


def test_a_camera_absent_from_the_provider_keeps_the_default(tmp_path):
    g = _groomer(tmp_path, 30, lambda: {})
    assert g._camera_groom_map() == {"cam": 30, "cam_sub": 30}


def test_a_never_groom_camera_yields_no_groom_candidates(tmp_path):
    """The end of the chain: 0 has to reach run_once as 'skip this camera'."""
    calls = []

    class _Idx:
        def get_cameras(self):
            return ["cam_sub"]

        def select_groomable(self, before, limit, camera=None):
            calls.append(camera)
            return []

    g = GroomingManager(_Idx(), groom_after_days=30, cameras_config=None,
                        groom_map_provider=lambda: {"cam_sub": 0})
    g.run_once(force=True)
    assert calls == [], "a never-groom camera must never be asked for candidates"


# ── The codec cache has to be able to go stale ──────────────────────────────
#
# "Codec is fixed by camera config" stopped being true when sub tracks arrived:
# a re-probe can swap `<slug>_sub` onto a different profile under the SAME
# recording name. A cache with no expiry then serves raw HEVC through the
# stream-copy path — the player shows black, with nothing logged.

@pytest.fixture
def srv(monkeypatch, tmp_path):
    from api import server
    server._codec_cache.clear()
    probes = []

    async def fake_exec(*args, **kw):
        probes.append(args[-1])

        class _P:
            returncode = 0
            async def communicate(self):
                codec = "hevc" if probes.count(args[-1]) > 1 else "h264"
                return (f"codec_name={codec}\nwidth=704\nheight=576\n".encode(), b"")
            def kill(self): pass
        return _P()

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_exec)
    server._probes = probes
    return server


@pytest.mark.asyncio
async def test_a_repeat_lookup_is_served_from_cache(srv):
    a = await srv._get_stream_info("cam_sub", "/d/s1.ts")
    b = await srv._get_stream_info("cam_sub", "/d/s1.ts")
    assert a == b == ("h264", 704, 576)
    assert len(srv._probes) == 1, "the cache still has to be a cache"


@pytest.mark.asyncio
async def test_an_expired_entry_is_re_probed(srv, monkeypatch):
    """The backstop: whatever changes a stream without telling us, the wrong
    answer expires instead of lasting until the process restarts."""
    await srv._get_stream_info("cam_sub", "/d/s1.ts")
    real = srv.time.time
    monkeypatch.setattr(srv.time, "time", lambda: real() + srv._CODEC_CACHE_TTL + 1)
    codec, _w, _h = await srv._get_stream_info("cam_sub", "/d/s1.ts")
    assert codec == "hevc", "a swapped profile must be picked up once the TTL lapses"


@pytest.mark.asyncio
async def test_invalidation_forces_an_immediate_re_probe(srv):
    """The mechanism, as opposed to the backstop: camera-mgmt drops the entry
    the moment it repoints a track, so the swap is visible at once."""
    await srv._get_stream_info("cam_sub", "/d/s1.ts")
    out = await srv.invalidate_codec_cache("cam_sub")
    assert out["had_entry"] is True
    codec, _w, _h = await srv._get_stream_info("cam_sub", "/d/s1.ts")
    assert codec == "hevc"


@pytest.mark.asyncio
async def test_invalidating_an_uncached_name_is_fine(srv):
    """Idempotent: "there is nothing stale here" is the outcome asked for."""
    out = await srv.invalidate_codec_cache("cam_sub")
    assert out["had_entry"] is False


# ── The engine's own three groom states ─────────────────────────────────────
#
# The chain is only as good as its store. These pin the end that actually holds
# the value, independently of how it got there.

@pytest.fixture
def engine(tmp_path, monkeypatch):
    from recorder import engine as eng
    # Workers spawn ffmpeg; this suite is about the override map, not recording.
    monkeypatch.setattr(eng, "StreamWorker", lambda **kw: type(
        "W", (), {"start": lambda s: None, "stop": lambda s: None})())
    cfg = {"recording": {"storage_path": str(tmp_path), "segment_duration": 60},
           "ffmpeg": {}, "cameras": []}
    return eng.RecordingEngine(cfg, index=None)


def test_never_groom_is_stored_as_a_real_override(engine):
    """0 has to survive as a value. A truthy test read it as "no preference"
    and the camera fell back to the appliance default — the rewrite it was
    opting out of."""
    engine.set_groom_after("cam_sub", 0)
    assert engine.get_groom_map() == {"cam_sub": 0}


def test_none_clears_the_override(engine):
    engine.set_groom_after("cam", 7)
    engine.set_groom_after("cam", None)
    assert "cam" not in engine.get_groom_map()


def test_zero_no_longer_clears(engine):
    """The behaviour change that made "never" sayable: 0 used to mean "clear",
    which is why it could not also mean "never"."""
    engine.set_groom_after("cam", 7)
    engine.set_groom_after("cam", 0)
    assert engine.get_groom_map() == {"cam": 0}


def test_a_runtime_added_track_can_be_added_as_never_groom(engine):
    """The path the reconcile loop actually uses — add_camera, not set_groom."""
    engine.add_camera("cam_sub", "rtsp://relay/cam_sub", retention_days=3,
                      groom_after_days=0)
    assert engine.get_groom_map()["cam_sub"] == 0


def test_a_runtime_camera_with_no_override_stores_none(engine):
    engine.add_camera("cam", "rtsp://relay/cam", retention_days=30,
                      groom_after_days=None)
    assert "cam" not in engine.get_groom_map()


# ── The playlist endpoint has to USE the selection rule ────────────────────
#
# select_track is tested above, but a test of the rule cannot tell you the
# playlist calls it — and "the surface stopped calling the shared definition"
# is how every one of this feature's bugs happened.

@pytest.mark.asyncio
async def test_the_playlist_serves_the_track_with_the_footage(tmp_path, monkeypatch):
    """A sub covering only the last hour must not win a two-hour request and
    hand back a playlist that reports the main's first hour as a gap."""
    from api import server

    W0, W1 = 1_000_000.0, 1_000_000.0 + 7200      # a two-hour ask
    seg_file = tmp_path / "s.ts"
    seg_file.write_bytes(b"0")

    def seg(start, dur):
        return Seg2(start, dur, str(seg_file))

    class _Idx:
        def get_cameras(self): return ["cam", "cam_sub"]
        def find_segments(self, name, a, b):
            if name == "cam_sub":                 # second hour only
                return [seg(W0 + 3600 + i * 60, 60) for i in range(60)]
            return [seg(W0 + i * 60, 60) for i in range(120)]   # the whole ask
        def get_recording_range(self, c): return (W0, W1)

    async def info(track, path):
        # The sub is the cheaper track to serve — which is exactly why ranking
        # on cost alone picked it.
        return ("h264", 704, 576) if track == "cam_sub" else ("hevc", 1920, 1080)

    monkeypatch.setattr(server, "_index", _Idx())
    monkeypatch.setattr(server, "_get_stream_info", info)
    monkeypatch.setattr(server, "_max_clip_seconds", 86400)

    resp = await server.hls_playlist(
        "cam", from_ts="1970-01-12T13:46:40Z", to_ts="1970-01-12T15:46:40Z", hevc=False)
    assert resp.headers["X-NVR-Track"] == "main", \
        "the track that actually covers the window has to win"
    assert resp.headers["X-NVR-Track-Coverage"].startswith("7200.0/")


class Seg2:
    """find_segments' shape: needs start/duration for coverage, filepath for
    the existence check the endpoint does."""
    def __init__(self, start_epoch, duration, filepath):
        self.start_epoch = start_epoch
        self.duration = duration
        self.filepath = filepath

    @property
    def end_epoch(self):
        return self.start_epoch + self.duration
