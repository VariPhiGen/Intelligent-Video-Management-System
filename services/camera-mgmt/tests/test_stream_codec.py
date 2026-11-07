"""Cameras stay on a VIDEO stream, never JPEG (services/stream_codec.py).

KEC A5000, 2026-10-21: four Panasonic/i-PRO WV-X6533L were registered on their
first ONVIF profile — JPEG. It played (so verification passed) and recorded, but
live view is HLS, which can't carry JPEG: every tile 404'd as "Signal lost".

Run (from services/camera-mgmt): python -m pytest tests/test_stream_codec.py -q
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services import stream_codec, substream  # noqa: E402

IP = "192.168.0.20"
JPEG_URL = f"rtsp://{IP}:554/MediaInput/jpeg/stream_1"
H264_URL = f"rtsp://{IP}:554/MediaInput/h264/stream_1"
H265_URL = f"rtsp://{IP}:554/MediaInput/h265/stream_1"
SMALL_H264 = f"rtsp://{IP}:554/MediaInput/h264/stream_2"

STREAMS = {
    JPEG_URL: {"codec": "mjpeg", "width": 1920, "height": 1080, "fps": 5},
    H265_URL: {"codec": "hevc", "width": 3840, "height": 2160, "fps": 25},
    H264_URL: {"codec": "h264", "width": 1920, "height": 1080, "fps": 25},
    SMALL_H264: {"codec": "h264", "width": 640, "height": 360, "fps": 25},
}


def _probe(streams):
    async def probe(url):
        return streams.get(substream._with_creds(stream_codec._strip_creds(url), None))
    return probe


def _cam(url=JPEG_URL, cands=(JPEG_URL, H265_URL, H264_URL, SMALL_H264), sub=None):
    return SimpleNamespace(slug="ipro-1", ip=IP, rtsp_url=url.replace("rtsp://", "rtsp://admin:pw@"),
                           rtsp_candidates=[{"url_raw": c, "profile": f"p{i}"} for i, c in enumerate(cands)],
                           sub_track=sub)


def test_best_video_prefers_h264_then_resolution_and_never_jpeg(monkeypatch):
    monkeypatch.setattr(substream, "probe_stream", _probe(STREAMS))
    idx, raw, info = asyncio.run(stream_codec.best_video(_cam()))
    assert raw == H264_URL and info["codec"] == "h264" and idx == 2      # H.264 1080p over 4K H.265 and 360p H.264


def test_registration_swaps_a_jpeg_profile_for_video_keeping_credentials(monkeypatch):
    monkeypatch.setattr(substream, "probe_stream", _probe(STREAMS))
    full = JPEG_URL.replace("rtsp://", "rtsp://admin:pw@")
    url, switched = asyncio.run(stream_codec.video_url_for(_cam(), full))
    assert url == H264_URL.replace("rtsp://", "rtsp://admin:pw@") and switched["codec"] == "h264"
    # A video profile is left exactly as chosen.
    chosen = H265_URL.replace("rtsp://", "rtsp://admin:pw@")
    assert asyncio.run(stream_codec.video_url_for(_cam(), chosen)) == (chosen, None)


def test_existing_jpeg_camera_is_repaired_in_place(monkeypatch):
    monkeypatch.setattr(substream, "probe_stream", _probe(STREAMS))
    cam = _cam()
    change = asyncio.run(stream_codec.repair_camera(cam))
    assert change["from_codec"] == "mjpeg" and change["codec"] == "h264"
    assert cam.rtsp_url == H264_URL.replace("rtsp://", "rtsp://admin:pw@")


def test_vendor_path_when_onvif_offers_only_jpeg(monkeypatch):
    monkeypatch.setattr(substream, "probe_stream", _probe(STREAMS))
    cam = _cam(cands=(JPEG_URL,))
    change = asyncio.run(stream_codec.repair_camera(cam))
    assert change["to"] == H264_URL                                     # i-PRO's /MediaInput/h264/stream_1


def test_unreachable_or_video_cameras_are_left_alone(monkeypatch):
    monkeypatch.setattr(substream, "probe_stream", _probe({}))          # camera down: nothing learned
    cam = _cam()
    assert asyncio.run(stream_codec.repair_camera(cam)) is None and "jpeg" in cam.rtsp_url
    monkeypatch.setattr(substream, "probe_stream", _probe(STREAMS))
    cam = _cam(url=H265_URL)
    assert asyncio.run(stream_codec.repair_camera(cam)) is None and cam.rtsp_url.endswith("h265/stream_1")
    # JPEG and nothing better anywhere: stays as it is.
    monkeypatch.setattr(substream, "probe_stream", _probe({JPEG_URL: STREAMS[JPEG_URL]}))
    cam = _cam(cands=(JPEG_URL,))
    assert asyncio.run(stream_codec.repair_camera(cam)) is None


def test_jpeg_is_never_a_sub_and_an_unrecorded_jpeg_sub_is_dropped():
    main = {"codec": "hevc", "width": 3840, "height": 2160}
    ok, why = substream.judge(main, {"codec": "mjpeg", "width": 640, "height": 360})
    assert not ok and "JPEG" in why                                     # used to pass on size alone
    cam = _cam(sub={"codec": "mjpeg", "recording_enabled": False})
    assert stream_codec.drop_jpeg_sub(cam) and cam.sub_track is None
    recorded = _cam(sub={"codec": "mjpeg", "recording_enabled": True})
    assert not stream_codec.drop_jpeg_sub(recorded) and recorded.sub_track     # a recording is the operator's call
