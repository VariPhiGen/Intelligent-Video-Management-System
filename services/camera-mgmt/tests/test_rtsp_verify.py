"""rtsp_verify.py — the device boundary where a wrong answer costs a camera.

WHY THIS ONE, out of the ~1,650 untested ONVIF lines. It is the point where the
product decides whether a camera works, and both directions are expensive:

  a false NO   onboarding rejects a camera that is fine, and the installer is
               on a ladder.
  a false YES  the camera registers, the relay stores a URL that will not play,
               and the failure surfaces later as a stream that never starts.

And it carries a correction that only exists because of real hardware: cameras
that serve RTSP over TLS still advertise plain `rtsp://` through ONVIF, so the
URL a scan stores is unplayable until the scheme is switched. `resolve_rtsp`
returns the scheme that ACTUALLY worked and the caller is told to store that —
returning the input URL on a successful TLS retry would be silently wrong in
exactly the case the retry exists for.

The retry is also deliberately not speculative: it is gated on a real TLS
handshake (milliseconds) so a dead camera costs one ffprobe timeout rather than
two. That is a performance property, but on a subnet scan with a dozen dead
addresses it is the difference between a scan that finishes and one an
installer abandons — so it is pinned like a correctness property.

Hermetic: ffprobe is never executed. `create_subprocess_exec` and the TLS
handshake are both stubbed, so these tests need no camera, no network and no
ffmpeg on the box.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services import rtsp_verify as rv  # noqa: E402

PLAIN = "rtsp://admin:secret@10.0.0.5:554/Streaming/Channels/101"
TLS = "rtsps://admin:secret@10.0.0.5:554/Streaming/Channels/101"


class FakeProc:
    """An ffprobe that has already decided how it will behave."""

    def __init__(self, returncode=0, stdout=b"codec_name=h264\n", hang=False):
        self.returncode = returncode
        self._stdout = stdout
        self._hang = hang
        self.killed = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(3600)
        return self._stdout, b""

    def kill(self):
        self.killed = True


def stub_ffprobe(monkeypatch, proc=None, raises=None):
    """Replace the subprocess launch; return the recorded argv list."""
    calls: list[tuple] = []

    async def _exec(*args, **kwargs):
        calls.append(args)
        if raises:
            raise raises
        return proc if proc is not None else FakeProc()

    monkeypatch.setattr(rv.asyncio, "create_subprocess_exec", _exec)
    return calls


# ── verify_rtsp: what counts as playable ───────────────────────────────────

class TestVerifyRtsp:
    @pytest.mark.asyncio
    async def test_a_video_stream_is_playable(self, monkeypatch):
        stub_ffprobe(monkeypatch, FakeProc(0, b"codec_name=h264\n"))
        assert await rv.verify_rtsp(PLAIN) is True

    @pytest.mark.asyncio
    async def test_a_non_zero_exit_is_not_playable(self, monkeypatch):
        stub_ffprobe(monkeypatch, FakeProc(1, b""))
        assert await rv.verify_rtsp(PLAIN) is False

    @pytest.mark.asyncio
    async def test_a_clean_exit_with_no_video_stream_is_not_playable(self, monkeypatch):
        # ffprobe can succeed against a URL that serves audio only, or answers
        # with metadata and no stream. "It connected" is not "it plays".
        stub_ffprobe(monkeypatch, FakeProc(0, b""))
        assert await rv.verify_rtsp(PLAIN) is False

    @pytest.mark.asyncio
    async def test_a_missing_ffprobe_is_false_not_a_crash(self, monkeypatch):
        # An image built without ffmpeg must fail onboarding honestly rather
        # than 500 the discovery endpoint.
        stub_ffprobe(monkeypatch, raises=FileNotFoundError("ffprobe"))
        assert await rv.verify_rtsp(PLAIN) is False

    @pytest.mark.asyncio
    async def test_a_hanging_probe_is_killed_and_reported_unplayable(self, monkeypatch):
        # A camera that accepts the connection and never answers would
        # otherwise hold a scan open until something else gives up.
        proc = FakeProc(hang=True)
        stub_ffprobe(monkeypatch, proc)
        monkeypatch.setattr(rv.settings, "discovery_rtsp_timeout", 0.01)
        assert await rv.verify_rtsp(PLAIN) is False
        assert proc.killed, "the timed-out ffprobe was left running"

    @pytest.mark.asyncio
    async def test_a_probe_that_already_exited_is_not_an_error(self, monkeypatch):
        # kill() on a reaped process raises ProcessLookupError; the race is
        # real and must not surface as a failed verification path.
        class GoneProc(FakeProc):
            def kill(self):
                raise ProcessLookupError()

        stub_ffprobe(monkeypatch, GoneProc(hang=True))
        monkeypatch.setattr(rv.settings, "discovery_rtsp_timeout", 0.01)
        assert await rv.verify_rtsp(PLAIN) is False

    @pytest.mark.asyncio
    async def test_certificate_verification_is_disabled(self, monkeypatch):
        # Camera certificates are self-signed, so an rtsps:// probe that
        # verified the chain would reject every working TLS camera.
        calls = stub_ffprobe(monkeypatch)
        await rv.verify_rtsp(TLS)
        argv = calls[0]
        assert "-tls_verify" in argv and argv[argv.index("-tls_verify") + 1] == "0"

    @pytest.mark.asyncio
    async def test_the_probe_uses_tcp_transport(self, monkeypatch):
        # UDP silently loses packets on a busy LAN and makes a working camera
        # look intermittently unplayable.
        calls = stub_ffprobe(monkeypatch)
        await rv.verify_rtsp(PLAIN)
        argv = calls[0]
        assert argv[argv.index("-rtsp_transport") + 1] == "tcp"

    @pytest.mark.asyncio
    async def test_the_url_is_passed_as_an_argument_never_through_a_shell(self, monkeypatch):
        # A camera password can contain anything. exec with an argv list has no
        # shell to quote for; a shell invocation here would be a command
        # injection reachable from a discovery response.
        calls = stub_ffprobe(monkeypatch)
        weird = "rtsp://admin:p@ss;rm -rf /@10.0.0.5:554/s"
        await rv.verify_rtsp(weird)
        assert weird in calls[0], "the URL was not passed as a discrete argument"


# ── resolve_rtsp: return the scheme that actually worked ───────────────────

class TestResolveRtsp:
    @pytest.mark.asyncio
    async def test_a_working_plain_url_is_returned_unchanged(self, monkeypatch):
        monkeypatch.setattr(rv, "verify_rtsp", lambda url: _true())
        ok, url = await rv.resolve_rtsp(PLAIN)
        assert (ok, url) == (True, PLAIN)

    @pytest.mark.asyncio
    async def test_a_tls_only_camera_comes_back_as_rtsps(self, monkeypatch):
        # THE POINT OF THE MODULE. The caller stores what is returned; giving
        # back the input here would store a URL that never plays.
        tried: list[str] = []

        async def _verify(url):
            tried.append(url)
            return url.startswith("rtsps://")

        monkeypatch.setattr(rv, "verify_rtsp", _verify)
        monkeypatch.setattr(rv.tlsutil, "cert_fingerprint", lambda h, p: _value("aa:bb"))

        ok, url = await rv.resolve_rtsp(PLAIN)
        assert ok is True
        assert url == TLS, "a TLS-only camera was reported with its unplayable URL"
        assert tried == [PLAIN, TLS]

    @pytest.mark.asyncio
    async def test_a_dead_camera_costs_one_probe_not_two(self, monkeypatch):
        # The TLS retry is gated on a handshake so a subnet full of dead
        # addresses does not double the scan time.
        tried: list[str] = []

        async def _verify(url):
            tried.append(url)
            return False

        monkeypatch.setattr(rv, "verify_rtsp", _verify)
        monkeypatch.setattr(rv.tlsutil, "cert_fingerprint", lambda h, p: _value(None))

        ok, url = await rv.resolve_rtsp(PLAIN)
        assert (ok, url) == (False, PLAIN)
        assert tried == [PLAIN], "a port that does not speak TLS was probed twice"

    @pytest.mark.asyncio
    async def test_a_camera_that_fails_both_schemes_returns_its_input(self, monkeypatch):
        monkeypatch.setattr(rv, "verify_rtsp", lambda url: _false())
        monkeypatch.setattr(rv.tlsutil, "cert_fingerprint", lambda h, p: _value("aa:bb"))
        ok, url = await rv.resolve_rtsp(PLAIN)
        assert (ok, url) == (False, PLAIN)

    @pytest.mark.asyncio
    async def test_an_rtsps_url_that_fails_is_not_retried(self, monkeypatch):
        # to_rtsps returns None for a URL that is already TLS; there is no
        # second scheme to try.
        tried: list[str] = []

        async def _verify(url):
            tried.append(url)
            return False

        monkeypatch.setattr(rv, "verify_rtsp", _verify)
        ok, url = await rv.resolve_rtsp(TLS)
        assert (ok, url) == (False, TLS)
        assert tried == [TLS]

    @pytest.mark.asyncio
    async def test_a_non_rtsp_url_is_not_retried(self, monkeypatch):
        tried: list[str] = []

        async def _verify(url):
            tried.append(url)
            return False

        monkeypatch.setattr(rv, "verify_rtsp", _verify)
        ok, url = await rv.resolve_rtsp("http://10.0.0.5/stream")
        assert ok is False
        assert len(tried) == 1

    @pytest.mark.asyncio
    async def test_the_handshake_targets_the_url_s_own_port(self, monkeypatch):
        probed: list[tuple] = []
        monkeypatch.setattr(rv, "verify_rtsp", lambda url: _false())
        monkeypatch.setattr(rv.tlsutil, "cert_fingerprint",
                            lambda h, p: _record(probed, h, p))
        await rv.resolve_rtsp("rtsp://10.0.0.5:8554/s")
        assert probed == [("10.0.0.5", 8554)]

    @pytest.mark.asyncio
    async def test_a_url_with_no_port_probes_the_rtsp_default(self, monkeypatch):
        probed: list[tuple] = []
        monkeypatch.setattr(rv, "verify_rtsp", lambda url: _false())
        monkeypatch.setattr(rv.tlsutil, "cert_fingerprint",
                            lambda h, p: _record(probed, h, p))
        await rv.resolve_rtsp("rtsp://10.0.0.5/s")
        assert probed == [("10.0.0.5", 554)]

    @pytest.mark.asyncio
    async def test_credentials_survive_the_scheme_correction(self, monkeypatch):
        # The corrected URL is what gets stored and handed to the relay. Losing
        # the userinfo would turn a working camera into an auth failure that
        # looks like a wrong password.
        monkeypatch.setattr(rv, "verify_rtsp",
                            lambda url: _value(url.startswith("rtsps://")))
        monkeypatch.setattr(rv.tlsutil, "cert_fingerprint", lambda h, p: _value("aa:bb"))
        _, url = await rv.resolve_rtsp(PLAIN)
        assert "admin:secret@" in url
        assert url.endswith("/Streaming/Channels/101")


# ── tiny async helpers, so the stubs above stay one-liners ─────────────────

async def _true():
    return True


async def _false():
    return False


async def _value(v):
    return v


async def _record(sink, host, port):
    sink.append((host, port))
    return None
