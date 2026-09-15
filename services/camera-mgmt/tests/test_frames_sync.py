"""Which cameras cost a decode.

The frame broker serves the Motion service and analytics, and analytics now
serves two consumers of its own — Smart Search and the CPU Activity Engine —
so the broker's camera set is the UNION of all of theirs. Getting this wrong is
expensive in one direction (decoding cameras nobody consumes) and silent in the
other (a camera whose motion events or AI activity events simply never
appear), so the rule is pinned here.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.services.frames_client import desired_sets  # noqa: E402

ACTIVITY = {"regions": {}, "activities": [{"type": "person_detection", "regions": [],
                                           "params": {}}]}


# (slug, enabled, motion_detection, search_indexing, search_domains, analytics_config)
def row(slug, en=True, motion=False, idx=False, dom=None, cfg=None):
    return (slug, en, motion, idx, dom if dom is not None else [], cfg)


def test_a_camera_only_motion_wants_is_decoded():
    d, a = desired_sets([row("m", motion=True)])
    assert d == {"m"} and a == set()


def test_a_camera_only_search_wants_is_decoded():
    d, a = desired_sets([row("s", idx=True, dom=["person"])])
    assert d == {"s"} and a == set()


def test_a_camera_only_an_ai_activity_wants_is_decoded():
    """The CPU Activity Engine runs on broker frames; without this the activity
    is configured, saved and never sees a single frame."""
    d, a = desired_sets([row("act", cfg=ACTIVITY)])
    assert d == {"act"} and a == set()


def test_a_camera_every_consumer_wants_appears_once():
    d, _ = desired_sets([row("b", motion=True, idx=True, dom=["person"], cfg=ACTIVITY)])
    assert d == {"b"}


def test_a_camera_neither_wants_is_not_decoded():
    """The whole saving. Decoding it 'just in case' reintroduces exactly the
    cost this replaces."""
    d, a = desired_sets([row("n")])
    assert d == set() and a == {"n"}


def test_search_indexing_without_domains_is_not_a_consumer():
    """Smart Search's own rule: no domains selected means nothing is indexed,
    so the frames would be decoded and then discarded."""
    d, a = desired_sets([row("x", idx=True, dom=[])])
    assert d == set() and a == {"x"}


def test_a_config_with_no_activities_is_not_a_consumer():
    d, a = desired_sets([row("x", cfg={"regions": {"r": {}}, "activities": []})])
    assert d == set() and a == {"x"}


def test_a_disabled_camera_is_never_decoded():
    """Disabled outranks every consumer — there is no stream to pull."""
    d, a = desired_sets([row("off", en=False, motion=True, idx=True,
                             dom=["person"], cfg=ACTIVITY)])
    assert d == set() and a == {"off"}


def test_the_two_sets_partition_the_registry():
    """Every registry camera is in exactly one, so reconcile can never both
    add and remove the same slug."""
    rows = [row("a", motion=True), row("b", idx=True, dom=["vehicle"]),
            row("c"), row("d", en=False, motion=True), row("e", cfg=ACTIVITY)]
    d, a = desired_sets(rows)
    assert d & a == set()
    assert d | a == {"a", "b", "c", "d", "e"}
