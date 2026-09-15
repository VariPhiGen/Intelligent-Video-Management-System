"""The rate this service reports is a copy of a rate something else runs at.

Nothing here samples any more: detection moved to services/analytics and
SEARCH_FRAME_SOURCE=none is the only valid mode. `max_sample_fps` survives as
the value /health publishes as `expected_source_fps` — the rate the frame
source is expected to run at — so its whole job is to be comparable with the
frames broker's rate. That makes a stale copy worse than no copy: the one field
meant to reveal a mismatch invents one. It did, when frames moved 2.0 -> 5.0
and this did not.

The pair is pinned in two places because the number lives in two kinds of file:
here, config.yaml against the dataclass; and in tests/test_compose_defaults.py
at the repo root, the compose default against the frames service's own.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from index.config import AppConfig, IngestConfig  # noqa: E402

CONFIG_YAML = str(Path(__file__).resolve().parents[1] / "config.yaml")


def clean(monkeypatch) -> None:
    """No inherited environment: these are about what the FILE ships."""
    monkeypatch.delenv("SEARCH_SAMPLE_FPS", raising=False)
    monkeypatch.delenv("SEARCH_FRAME_SOURCE", raising=False)


def test_the_shipped_file_loads_with_no_environment_at_all(monkeypatch):
    """The image's CMD is `--config config.yaml`, so this file has to be valid
    on its own. It shipped `frame_source: sampler` after that became a refusal
    — the service started only because compose happens to set
    SEARCH_FRAME_SOURCE=none, and a plain `python main.py` raised ValueError."""
    clean(monkeypatch)
    assert AppConfig.from_yaml(CONFIG_YAML).ingest.frame_source == "none"


def test_the_shipped_rate_is_the_code_default(monkeypatch):
    """config.yaml wins over the dataclass — the image runs
    `--config config.yaml` — so changing one and not the other ships the old
    number while the code appears to say otherwise."""
    clean(monkeypatch)
    assert (AppConfig.from_yaml(CONFIG_YAML).ingest.max_sample_fps
            == IngestConfig().max_sample_fps)


def test_the_environment_overrides_the_file(monkeypatch):
    """Compose sets it explicitly, so the environment is what actually runs on
    an appliance."""
    clean(monkeypatch)
    monkeypatch.setenv("SEARCH_SAMPLE_FPS", "2")
    assert AppConfig.from_yaml(CONFIG_YAML).ingest.max_sample_fps == 2.0
