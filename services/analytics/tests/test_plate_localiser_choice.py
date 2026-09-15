"""Which plate localiser `build()` picks — index/plates.py.

The precedence IS the product shape (open baseline, paid accuracy): site-tuned
weights beat the auto-downloading open baseline, the baseline beats nothing,
and a weights load that fails must NOT fall back — the operator pointed at a
specific model, and silently substituting a generic one makes "my custom model
is misconfigured" look like "my custom model is mediocre".

No model is loaded: both localiser classes are monkeypatched to sentinels, so
what is pinned is purely the choice. Runs anywhere.

Run: python3 -m pytest tests -q
"""
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analytics import plates  # noqa: E402
from analytics.config import PlateConfig  # noqa: E402


class _Sentinel:
    def __init__(self, *a, **kw):
        self.args = a

    def locate(self, crop):
        return []


@pytest.fixture
def stubbed(monkeypatch):
    """Replace both localisers and the reader so build() is pure choice."""
    made = {}

    class Tuned(_Sentinel):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            made["localiser"] = "tuned"

    class Baseline(_Sentinel):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            made["localiser"] = "baseline"

    class Reader:
        def __init__(self, model_name, device, localiser, *a, **kw):
            made["reader_localiser"] = localiser

    monkeypatch.setattr(plates, "UltralyticsPlateLocaliser", Tuned)
    monkeypatch.setattr(plates, "OnnxPlateLocaliser", Baseline)
    monkeypatch.setattr(plates, "PlateReader", Reader)
    return made


def test_the_open_baseline_makes_plates_work_with_nothing_supplied(stubbed):
    """A clean install has no weights file — the default config alone must
    yield an active reader, or first-start ANPR is back to a banner."""
    assert PlateConfig().localiser_model, "the default baseline must be set"
    assert plates.build(PlateConfig(), None) is not None
    assert stubbed["localiser"] == "baseline"


def test_site_tuned_weights_beat_the_baseline(stubbed):
    cfg = replace(PlateConfig(), localiser_weights="/models/my-anpr.pt")
    assert plates.build(cfg, None) is not None
    assert stubbed["localiser"] == "tuned"


def test_both_empty_means_inactive(stubbed):
    cfg = replace(PlateConfig(), localiser_weights="", localiser_model="")
    assert plates.build(cfg, None) is None
    assert "localiser" not in stubbed


def test_failed_tuned_weights_do_not_fall_back_to_the_baseline(stubbed, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("bad weights file")
    monkeypatch.setattr(plates, "UltralyticsPlateLocaliser", boom)
    cfg = replace(PlateConfig(), localiser_weights="/models/broken.pt")
    assert plates.build(cfg, None) is None, (
        "a failed site-tuned load must surface as inactive, not silently "
        "degrade to the generic baseline")
    assert stubbed.get("localiser") != "baseline"
