"""face_weights.py — what may and may not land in the models volume.

WHY THIS IS RISKY. This is the one place in the service that writes a file it
downloaded from the internet and then hands it to a model loader. Three of the
tests below are about what must NOT happen:

  * a file whose digest does not match must never reach /models, because the
    next start would load it as a model — a truncated download and a captive
    portal's login page both arrive as "a file", and neither is a model;
  * a path the operator chose must never be overwritten by an upstream file we
    guessed from its name;
  * a failure must never raise, because the caller is a warm-up thread whose
    contract is that a missing face model costs face search and nothing else.

Hermetic: urlopen is replaced, nothing leaves the process.

Run (from services/smartsearch): python -m pytest tests -q
"""
from __future__ import annotations

import hashlib
import io
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from index import face_weights as fw  # noqa: E402

YUNET = "face_detection_yunet_2023mar.onnx"
SFACE = "face_recognition_sface_2021dec.onnx"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """Each test gets a clean attempt cache and no ambient opt-out."""
    monkeypatch.setattr(fw, "_attempted", set())
    for var in ("SEARCH_FACE_AUTO_DOWNLOAD", "ANALYTICS_FACE_AUTO_DOWNLOAD"):
        monkeypatch.delenv(var, raising=False)


def serve(monkeypatch, payload: bytes, calls: list | None = None):
    def _urlopen(url, timeout=None):
        if calls is not None:
            calls.append(url)
        return FakeResponse(payload)
    monkeypatch.setattr(fw.urllib.request, "urlopen", _urlopen)


def with_digest(monkeypatch, name: str, payload: bytes) -> None:
    """Point `name`'s expected digest at `payload`, so a fetch can succeed."""
    url, _ = fw._MODELS[name]
    monkeypatch.setitem(fw._MODELS, name,
                        (url, hashlib.sha256(payload).hexdigest()))


class TestFetching:
    def test_a_missing_model_is_downloaded_and_installed(self, tmp_path, monkeypatch):
        payload = b"onnx-bytes"
        with_digest(monkeypatch, YUNET, payload)
        serve(monkeypatch, payload)

        target = tmp_path / YUNET
        fw.ensure(str(target))
        assert target.read_bytes() == payload

    def test_a_model_already_present_is_not_refetched(self, tmp_path, monkeypatch):
        target = tmp_path / YUNET
        target.write_bytes(b"already here")
        calls: list = []
        serve(monkeypatch, b"replacement", calls)

        fw.ensure(str(target))
        assert target.read_bytes() == b"already here"
        assert calls == [], "re-downloaded a model that was already installed"

    def test_both_models_are_fetched_in_one_call(self, tmp_path, monkeypatch):
        for name in (YUNET, SFACE):
            with_digest(monkeypatch, name, b"x")
        calls: list = []
        serve(monkeypatch, b"x", calls)

        fw.ensure(str(tmp_path / YUNET), str(tmp_path / SFACE))
        assert len(calls) == 2


class TestWhatMustNotLand:
    def test_a_digest_mismatch_installs_nothing(self, tmp_path, monkeypatch):
        # The recorded digest stays; the server sends something else. A
        # truncated download and a captive portal both look like this.
        serve(monkeypatch, b"not the model you were expecting")

        target = tmp_path / YUNET
        fw.ensure(str(target))
        assert not target.exists(), "a file that failed verification was installed"

    def test_a_digest_mismatch_leaves_no_partial_file_behind(self, tmp_path, monkeypatch):
        serve(monkeypatch, b"wrong")
        fw.ensure(str(tmp_path / YUNET))
        assert list(tmp_path.iterdir()) == [], "temp file left in the models volume"

    def test_an_operator_supplied_path_is_never_fetched(self, tmp_path, monkeypatch):
        # Not a name we ship a default for. Guessing a URL from a filename is
        # how you install the wrong model.
        calls: list = []
        serve(monkeypatch, b"x", calls)

        target = tmp_path / "our_own_sface.onnx"
        fw.ensure(str(target))
        assert calls == [] and not target.exists()

    def test_the_opt_out_is_honoured(self, tmp_path, monkeypatch):
        # An air-gapped site that stages weights by hand must not have the
        # service reaching for the network on every warm-up.
        monkeypatch.setenv("SEARCH_FACE_AUTO_DOWNLOAD", "false")
        calls: list = []
        serve(monkeypatch, b"x", calls)

        fw.ensure(str(tmp_path / YUNET))
        assert calls == []


class TestFailureIsSurvivable:
    def test_a_blocked_network_does_not_raise(self, tmp_path, monkeypatch):
        def _boom(url, timeout=None):
            raise urllib.error.URLError("egress blocked")
        monkeypatch.setattr(fw.urllib.request, "urlopen", _boom)

        fw.ensure(str(tmp_path / YUNET))  # must simply return
        assert not (tmp_path / YUNET).exists()

    def test_a_failed_fetch_is_not_retried_in_this_process(self, tmp_path, monkeypatch):
        # Without this, a face query on an egress-blocked box re-attempts a
        # 37 MB download on every search.
        calls: list = []

        def _boom(url, timeout=None):
            calls.append(url)
            raise urllib.error.URLError("egress blocked")
        monkeypatch.setattr(fw.urllib.request, "urlopen", _boom)

        target = str(tmp_path / YUNET)
        fw.ensure(target)
        fw.ensure(target)
        fw.ensure(target)
        assert len(calls) == 1


class TestRegistry:
    def test_the_shipped_defaults_are_both_fetchable(self):
        # The config's default paths and this registry's keys are the same two
        # names, or auto-download silently covers nothing.
        from index.config import FaceConfig
        cfg = FaceConfig()
        import os
        for path in (cfg.detector_weights, cfg.recogniser_weights):
            assert os.path.basename(path) in fw._MODELS, (
                f"{path} is a shipped default that nothing will fetch")

    def test_every_digest_is_a_sha256(self):
        for name, (url, digest) in fw._MODELS.items():
            assert len(digest) == 64 and int(digest, 16) >= 0, name
            assert url.startswith("https://"), name
