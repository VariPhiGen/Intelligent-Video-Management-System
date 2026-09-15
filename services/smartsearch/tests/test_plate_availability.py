"""Does /health tell the truth about whether this deployment reads plates?

SEMANTICS CHANGED 2026-09-09 (`9f5a5ec`). Plate reading MOVED OUT of Smart
Search into services/analytics, so `ModelPool.plates_available` no longer probes
a loaded model — this service has none. It now answers one question, from
configuration alone: "does this deployment do ANPR at all", which is what the UI
needs to say "not enabled here" instead of showing an empty result and letting
the operator read it as "this vehicle was never seen".

Two tests here used to pin the OLD contract — that a load which ran and produced
no reader outranks the config's optimism, for the egress-blocked appliance whose
weights download fails. They failed from `9f5a5ec` until this rewrite, and were
merged red in PR #29.

That concern did not go away, it MOVED. The load either succeeds or does not in
analytics now (`analytics/plates.py build()` returns None and logs "plate
reading INACTIVE"), and analytics reports it as `plates_loaded` /`plates_active`
on its own /health. It is pinned there, in
`services/analytics/tests/test_plate_health.py` — deleting these tests without
that would have removed the safety net rather than relocating it.

Run: python3 -m pytest tests -q   (from services/smartsearch)
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from index.config import AppConfig                                # noqa: E402
from index.models import ModelPool                                # noqa: E402


def _pool(**plates) -> ModelPool:
    cfg = AppConfig()
    for k, v in plates.items():
        setattr(cfg.plates, k, v)
    return ModelPool(cfg, store=SimpleNamespace(), writer=SimpleNamespace())


def test_before_any_load_configuration_is_the_only_signal():
    """The hibernation case, and a cold start: nothing has been attempted, so
    the configured intent is the best available answer."""
    assert _pool(enabled=True, localiser_model="yolo-v9-t").plates_available is True


def test_disabled_in_configuration_is_unavailable():
    assert _pool(enabled=False).plates_available is False


def test_no_localiser_at_all_is_unavailable():
    """Recognition alone cannot find a plate on a whole car."""
    assert _pool(enabled=True, localiser_weights="", localiser_model="").plates_available is False


def test_the_open_baseline_alone_counts_as_configured():
    """`localiser_model` ships non-empty, so a default deployment does ANPR."""
    assert _pool(enabled=True, localiser_model="yolo-v9-t").plates_available is True


def test_site_tuned_weights_count_too():
    assert _pool(enabled=True, localiser_weights="/models/site.pt",
                 localiser_model="").plates_available is True


def test_this_service_no_longer_probes_a_loaded_reader():
    """The semantics change, stated as a test.

    Smart Search has no plate reader to ask any more — it embeds crops that
    analytics already read. So the answer must depend on CONFIGURATION ONLY,
    and stay stable whatever a stale model attribute happens to say. Whether a
    reader actually loaded is analytics' question, answered on its /health.
    """
    pool = _pool(enabled=True, localiser_model="yolo-v9-t")
    before = pool.plates_available
    pool._plates = SimpleNamespace(active=False)      # ignored now
    pool._plates_load_failed = True                   # ignored now
    assert pool.plates_available is before is True


def test_hibernation_does_not_change_the_answer():
    """The distinction the UI depends on: a hibernating service still reads
    plates, it is simply not reading one right now."""
    pool = _pool(enabled=True, localiser_model="yolo-v9-t")
    pool._plates = None
    assert pool.plates_available is True
