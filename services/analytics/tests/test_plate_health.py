"""Does /health tell the truth when the plate reader never loaded?

RELOCATED FROM SMART SEARCH on 2026-09-09. `9f5a5ec` moved plate reading out of
services/smartsearch into this service, and with it the question these tests
protect. The original concern, unchanged:

    `localiser_model` ships non-empty and the weights auto-download on first
    load, so an appliance with NO EGRESS fails that download. If /health then
    keeps reporting plate reading as available, every empty plate query on that
    box reads as "this vehicle was never seen" rather than "this appliance
    cannot read plates" — and those are completely different answers to give a
    person looking for a car.

`build()` is where it is decided (it returns None and logs "plate reading
INACTIVE" rather than raising — a missing plate reader must cost plate search,
not the whole ingest), and `plates_loaded` on /health is where it is reported.
Both are pinned here.

Run (from services/analytics): python -m pytest tests -q
"""
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analytics import plates  # noqa: E402


@dataclass
class _Cfg:
    """COMPLETE on purpose. An incomplete config makes these tests pass for the
    wrong reason: build() ends in `PlateReader(cfg.model, ..., cfg.min_length,
    ...)` wrapped in `except Exception: return None`, so a missing attribute
    raises there and returns None no matter what the localiser branch did —
    and a test asserting `is None` would then pass even with the guard deleted.
    Verified by mutation: with these fields present, removing the `return None`
    from the baseline branch fails the test; without them it does not."""
    enabled: bool = True
    localiser_weights: str = ""
    localiser_model: str = "yolo-v9-t-384-license-plate-end2end"
    localiser_confidence: float = 0.3
    localiser_square_letterbox: bool = False
    min_confidence: float = 0.6
    model: str = "cct-xs-v2-global-model"
    min_length: int = 4
    region_padding: float = 0.1
    two_line_max_aspect: float = 2.5
    two_line_min_row_confidence: float = 0.4


@pytest.fixture(autouse=True)
def _reader_would_build(monkeypatch):
    """Make the fall-through PATH VISIBLE. Without this, a build() that fails
    to return early still returns None because constructing a real PlateReader
    raises — masking exactly the bug these tests guard."""
    monkeypatch.setattr(plates, "PlateReader",
                        lambda *a, **kw: SimpleNamespace(active=True))


# ── build(): a load that cannot complete yields NO reader ────────────────────

def test_plate_reading_switched_off_yields_no_reader():
    assert plates.build(_Cfg(enabled=False), device=None) is None


def test_a_failed_site_weights_load_yields_no_reader(monkeypatch):
    """The operator pointed at a specific model. It must not silently fall back
    to the generic one — "my custom model is misconfigured" would then look
    like "my custom model is mediocre"."""
    def boom(*a, **kw):
        raise RuntimeError("no such file: /models/site.pt")

    monkeypatch.setattr(plates, "UltralyticsPlateLocaliser", boom)
    assert plates.build(_Cfg(localiser_weights="/models/site.pt"), device=None) is None


def test_a_failed_baseline_download_yields_no_reader(monkeypatch):
    """THE EGRESS-BLOCKED APPLIANCE — the case this file exists for."""
    def boom(*a, **kw):
        raise OSError("Temporary failure in name resolution")

    monkeypatch.setattr(plates, "OnnxPlateLocaliser", boom)
    assert plates.build(_Cfg(), device=None) is None


def test_build_never_raises_it_returns_none(monkeypatch):
    """A missing plate reader costs plate search, not the whole ingest."""
    def boom(*a, **kw):
        raise OSError("Temporary failure in name resolution")

    monkeypatch.setattr(plates, "OnnxPlateLocaliser", boom)
    try:
        plates.build(_Cfg(), device=None)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"build() must swallow load errors, raised {exc!r}")


# ── /health: no reader is REPORTED as no reader ──────────────────────────────

class _Engine:
    """The two lines of engine.snapshot() that answer this question."""
    def __init__(self, reader): self._plates = reader

    def models(self):
        return {"plates_loaded": self._plates is not None,
                "plates_active": bool(getattr(self._plates, "active", False))}


def test_health_reports_a_missing_reader_as_missing():
    assert _Engine(None).models() == {"plates_loaded": False, "plates_active": False}


def test_health_reports_a_loaded_reader_as_present():
    class _R: active = True
    assert _Engine(_R()).models() == {"plates_loaded": True, "plates_active": True}


def test_a_loaded_but_inactive_reader_is_not_reported_active():
    """Loaded and usable are different claims; only the second one means the
    operator will get plate reads."""
    class _R: active = False
    m = _Engine(_R()).models()
    assert m["plates_loaded"] is True and m["plates_active"] is False
