"""Which cameras cost a detector pass — and what analytics is told about them.

Analytics has two consumers now: Smart Search (the objects it records) and the
CPU Activity Engine (the AI activities configured in AI Config). A camera is
analysed when EITHER wants it, and two failures are silent, so both are pinned:

  * a camera the index wants but analytics ignores is a person no search can
    return — Smart Search's own rule must survive unchanged inside this one;
  * a camera with an activity configured but not indexed must be analysed for
    that activity WITHOUT quietly starting to index it.

What travels with the registration is the stored analytics_config itself, and
drift is spotted by a fingerprint both services compute identically.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import settings  # noqa: E402
from backend.services import analytics_client as ac  # noqa: E402
from backend.services.analytics_client import (Desired, config_fingerprint,  # noqa: E402
                                               desired_sets)

ZONE = {"kind": "zone", "name": "Zone 1", "color": "#4fd1c5",
        "points": [[0.1, 0.2], [0.9, 0.2], [0.5, 0.9]], "direction": None}
PERSON_DETECTION = {
    "regions": {"region_1": ZONE},
    "activities": [{"type": "person_detection", "regions": ["region_1"],
                    "params": {"active_hours": None, "active_days": ["Mon", "Tue"],
                               "person_classes": ["person"], "min_confidence": 0.5}}],
}


# (slug, enabled, search_indexing, search_domains, analytics_config)
def row(slug, en=True, idx=True, dom=("person", "vehicles"), cfg=None):
    return (slug, en, idx, list(dom), cfg)


# ── Smart Search's rule, unchanged inside the union ─────────────────────────

def test_an_indexed_camera_is_analysed():
    d, a = desired_sets([row("a")])
    assert d == {"a": Desired(["person", "vehicles"], {"regions": {}, "activities": []})}
    assert a == set()


def test_domains_are_passed_through():
    """Analytics applies them: a person-only camera must not pay to crop,
    track and plate-read vehicles the index would drop anyway."""
    d, _ = desired_sets([row("a", dom=("person",))])
    assert d["a"].domains == ["person"]


def test_a_camera_neither_consumer_wants_is_not_analysed():
    """The whole saving. Detecting on it would produce results with nowhere
    to go."""
    d, a = desired_sets([row("a", idx=False)])
    assert d == {} and a == {"a"}


def test_indexing_on_with_no_domains_is_not_a_consumer():
    """Smart Search's own rule: no domains selected means nothing is indexed."""
    d, a = desired_sets([row("a", dom=())])
    assert d == {} and a == {"a"}


def test_a_disabled_camera_is_never_analysed():
    d, a = desired_sets([row("a", en=False, cfg=PERSON_DETECTION)])
    assert d == {} and a == {"a"}


def test_the_indexing_half_matches_smart_searchs_own_rule():
    """If the indexing half ever diverges from Smart Search's, some cameras are
    detected and never stored. Compared against the real function."""
    import inspect

    from backend.services import smartsearch_sync
    src = inspect.getsource(smartsearch_sync.reconcile_loop)
    assert "en and idx and dom" in src, (
        "Smart Search's desired-set rule changed shape; check that "
        "analytics_client.wants still contains it")
    assert ac.wants(True, True, ["person"], None) and not ac.wants(True, True, [], None)
    assert not ac.wants(True, False, ["person"], None) and not ac.wants(False, True, ["person"], None)


# ── the CPU Activity Engine's half ──────────────────────────────────────────

def test_a_camera_with_an_activity_is_analysed_without_being_indexed():
    d, a = desired_sets([row("gate", idx=False, cfg=PERSON_DETECTION)])
    assert a == set()
    assert d["gate"].domains == []                        # Smart Search gets nothing
    assert d["gate"].analytics_config == PERSON_DETECTION


def test_an_indexed_camera_with_an_activity_keeps_its_search_domains():
    d, _ = desired_sets([row("gate", dom=("vehicles",), cfg=PERSON_DETECTION)])
    assert d["gate"] == Desired(["vehicles"], PERSON_DETECTION)


def test_opted_out_of_indexing_but_domains_still_ticked_indexes_nothing():
    """The stored domains outlive the opt-out; they must not ride along."""
    d, _ = desired_sets([row("gate", idx=False, dom=("person",), cfg=PERSON_DETECTION)])
    assert d["gate"].domains == []


def test_an_empty_or_malformed_activity_list_is_not_a_consumer():
    for cfg in ({}, {"regions": {"r": ZONE}, "activities": []},
                {"activities": [{"regions": ["r"]}]}, "not a dict"):
        d, a = desired_sets([row("x", idx=False, cfg=cfg)])
        assert d == {} and a == {"x"}, cfg


def test_only_the_parts_activities_run_on_are_carried():
    cfg = {**PERSON_DETECTION, "something_else": 1}
    d, _ = desired_sets([row("gate", idx=False, cfg=cfg)])
    assert set(d["gate"].analytics_config) == {"regions", "activities"}


def test_the_two_sets_partition_the_registry():
    """Every registry camera is in exactly one, so reconcile can never both
    add and remove the same slug."""
    rows = [row("a"), row("b", idx=False), row("c", en=False),
            row("d", dom=("vehicles",)), row("e", idx=False, cfg=PERSON_DETECTION)]
    d, a = desired_sets(rows)
    assert set(d) & a == set()
    assert set(d) | a == {"a", "b", "c", "d", "e"}


# ── drift detection shared with analytics ───────────────────────────────────

def test_the_fingerprint_is_pinned_and_matches_analytics():
    """services/analytics/tests/test_activities.py pins the same digests for
    the same input. Change one side's canonical form alone and a test fails."""
    assert config_fingerprint(PERSON_DETECTION) == "a1a5552606b565b1"
    assert config_fingerprint({}) == config_fingerprint(None) == "3e3462a9e90b2edd"


@pytest.fixture
def calls(monkeypatch):
    got = {"add": [], "remove": []}

    async def add(slug, domains=None, analytics_config=None):
        got["add"].append((slug, domains, analytics_config))
        return True

    async def remove(slug):
        got["remove"].append(slug)
        return True

    monkeypatch.setattr(ac, "add_camera", add)
    monkeypatch.setattr(ac, "remove_camera", remove)
    return got


def _serve(monkeypatch, current):
    async def listing():
        return current
    monkeypatch.setattr(ac, "list_cameras", listing)


@pytest.mark.asyncio
async def test_reconcile_adds_a_missing_camera_with_its_config(monkeypatch, calls):
    _serve(monkeypatch, {})
    d, a = desired_sets([row("gate", idx=False, cfg=PERSON_DETECTION)])
    await ac.reconcile(d, a)
    assert calls["add"] == [("gate", [], PERSON_DETECTION)]


@pytest.mark.asyncio
async def test_reconcile_leaves_a_camera_that_already_matches(monkeypatch, calls):
    _serve(monkeypatch, {"gate": ([], config_fingerprint(PERSON_DETECTION))})
    d, a = desired_sets([row("gate", idx=False, cfg=PERSON_DETECTION)])
    await ac.reconcile(d, a)
    assert calls == {"add": [], "remove": []}


@pytest.mark.asyncio
async def test_reconcile_reasserts_a_camera_whose_activity_config_drifted(monkeypatch, calls):
    _serve(monkeypatch, {"gate": ([], "stale-fingerprint")})
    d, a = desired_sets([row("gate", idx=False, cfg=PERSON_DETECTION)])
    await ac.reconcile(d, a)
    assert calls["add"] == [("gate", [], PERSON_DETECTION)]


@pytest.mark.asyncio
async def test_reconcile_removes_a_camera_nobody_wants_any_more(monkeypatch, calls):
    _serve(monkeypatch, {"gate": ([], config_fingerprint(PERSON_DETECTION))})
    d, a = desired_sets([row("gate", idx=False, cfg={})])
    await ac.reconcile(d, a)
    assert calls["remove"] == ["gate"]


@pytest.mark.asyncio
async def test_push_camera_applies_a_save_immediately(monkeypatch, calls):
    monkeypatch.setattr(settings, "analytics_api_url", "http://analytics:8015")
    monkeypatch.setattr(settings, "analytics_sync_enabled", True)
    await ac.push_camera("gate", True, False, ["person"], PERSON_DETECTION)
    await ac.push_camera("gate", True, False, ["person"], {"regions": {}, "activities": []})
    assert calls["add"] == [("gate", [], PERSON_DETECTION)]
    assert calls["remove"] == ["gate"]


@pytest.mark.asyncio
async def test_push_camera_does_nothing_without_an_analytics_service(monkeypatch, calls):
    monkeypatch.setattr(settings, "analytics_api_url", "")
    await ac.push_camera("gate", True, False, [], PERSON_DETECTION)
    assert calls == {"add": [], "remove": []}
