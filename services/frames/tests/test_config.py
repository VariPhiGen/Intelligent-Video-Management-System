"""The sample rate is decided in one place — and written down in three.

CaptureConfig calls itself "THE ONE PLACE THE SAMPLE RATE IS DECIDED", but the
same number also ships in config.yaml and as the compose fallback. a89fff5
moved all three from 2 to 5 FPS by hand; nothing would have noticed if it had
moved two. These pin config.yaml to the dataclass; tests/test_compose_defaults.py
pins compose to config.yaml.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from broker.config import AppConfig, CaptureConfig  # noqa: E402

CONFIG_YAML = str(Path(__file__).resolve().parents[1] / "config.yaml")


def test_the_shipped_rate_is_the_code_default(monkeypatch):
    monkeypatch.delenv("FRAMES_SAMPLE_FPS", raising=False)
    assert (AppConfig.from_yaml(CONFIG_YAML).capture.sample_fps
            == CaptureConfig().sample_fps)


def test_the_environment_overrides_the_file(monkeypatch):
    """Environment wins, so a site can drop back to 2 FPS without a rebuild."""
    monkeypatch.setenv("FRAMES_SAMPLE_FPS", "2")
    assert AppConfig.from_yaml(CONFIG_YAML).capture.sample_fps == 2.0
