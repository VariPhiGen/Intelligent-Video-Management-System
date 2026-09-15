"""Camera passwords must not reach the NVR's logs — including via ffmpeg.

Two places emit a credentialed URL. The command line was already redacted; the
stderr relay was not, and that is the leakier of the two: ffmpeg repeats the
full input URL in its 'Input #0' banner and in every connection error, so a
camera that will not connect logs the password once per reconnect attempt —
densest exactly while an operator is watching.

The helper is a copy of camera-mgmt's urlutil.redact_credentials (this service
cannot import it), so these tests also exist to catch the two drifting apart:
the same cases are asserted in camera-mgmt/tests/test_urlutil_redact.py.

Run (from services/nvr): python -m pytest tests -q
"""
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recorder.stream_worker import StreamWorker, _redact_credentials  # noqa: E402

URL = "rtsp://admin:my/pass@10.0.0.5:554/main"


def test_a_raw_slash_in_the_password_is_masked():
    assert _redact_credentials(URL) == "rtsp://admin:***@10.0.0.5:554/main"


def test_a_raw_at_sign_does_not_leak_its_tail():
    out = _redact_credentials("rtsp://admin:p@ss@10.0.0.5:554/main")
    assert out == "rtsp://admin:***@10.0.0.5:554/main"


def test_ffmpegs_own_log_prefix_does_not_confuse_the_match():
    """ffmpeg prefixes lines with '[rtsp @ 0x...]' — a stray '@' in the
    surrounding text must not extend or break the userinfo match."""
    line = f"[rtsp @ 0x557d] method DESCRIBE failed: 401 Unauthorized {URL}"
    out = _redact_credentials(line)
    assert "my/pass" not in out
    assert "[rtsp @ 0x557d] method DESCRIBE failed: 401 Unauthorized" in out


def test_the_stderr_relay_actually_redacts(caplog):
    """The wiring, not the helper: _log_stderr is the path that leaks, and a
    correct helper it does not call protects nothing."""
    worker = StreamWorker.__new__(StreamWorker)      # no ffmpeg, no config
    worker.camera = "gate-a1b2"
    worker._process = SimpleNamespace(
        stderr=iter([f"Input #0, rtsp, from '{URL}':\n".encode()]))

    with caplog.at_level(logging.DEBUG, logger="recorder.stream_worker"):
        StreamWorker._log_stderr(worker)

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "my/pass" not in logged, "the raw password reached the log"
    assert "rtsp://admin:***@10.0.0.5:554/main" in logged
