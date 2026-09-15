"""queries.py — the read path, which is where a silent wrong answer lives.

WHY THIS FILE IS THE FIRST P4 TARGET. Everything else in this service fails
loudly: an indexing error is a log line and a stalled queue, a model that will
not load is a red /health. The read path is the one place a defect produces a
plausible, confident, wrong result — and the module's own docstring is a list of
ways that has already happened, each of which "does not fail loudly".

546 lines, 17 endpoints behind them, and no test until now.

WHAT IS ASSERTED, AND WHY IT CAN BE. Every dangerous behaviour here is decided
when the SQL is BUILT, not when Postgres runs it: whether the camera filter is
applied at all, whether expired rows are excluded, whether the scoped flag is
set. So these tests give QueryService a Store that records the SQL and the
params instead of executing them, and read the WHERE clause back. That is not a
weaker test than a database — for these invariants it is a stronger one, because
"the filter was omitted" is visible directly rather than inferred from rows that
happen not to exist in a fixture.

Two behaviours genuinely need rows (grouping, ranking, bucket shaping), and
those get canned rows through the same seam.

No database, no encoder, no network.

Run (from services/smartsearch): python -m pytest tests -q
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from index.queries import (  # noqa: E402
    EncoderUnavailable,
    QueryService,
    _RECENT_PLATES,
    _TOP_PLATES,
)

UTC = timezone.utc


# ── The seams ──────────────────────────────────────────────────────────────

class RecordingStore:
    """A Store that answers with canned rows and remembers what it was asked.

    `queries` is every (sql, params) pair in order. Tests assert on the SQL
    text because the SQL text is the behaviour: a missing predicate is a
    missing filter.
    """

    def __init__(self, rows=None, by_match: dict[str, list] | None = None):
        self.queries: list[tuple[str, dict]] = []
        self._rows = rows if rows is not None else []
        #: substring of the SQL -> rows, for the multi-query methods.
        self._by_match = by_match or {}

    def fetch(self, sql: str, params: dict | None = None) -> list[dict]:
        self.queries.append((sql, dict(params or {})))
        for needle, rows in self._by_match.items():
            if needle in sql:
                return rows
        return self._rows

    # -- helpers the tests read through --------------------------------------
    #: The widening window's own bounds, which differ from slice to slice by
    #: design and say nothing about whether a FILTER was applied.
    _WINDOW_BOUND = re.compile(r"\s*AND ts [<>]=? %\(w_(?:since|until)\)s")

    def _filters_of(self, sql: str) -> str:
        return self._WINDOW_BOUND.sub("", self.where_of(sql))

    @property
    def sql(self) -> str:
        """The statement issued — or the first of several that filter alike.

        "Exactly one" stopped being true when the recent window arrived: a
        vector search reads a reference ranking and then one slice per window
        step (index/queries.py `_recent_first`). Asserting instead that every
        statement carries the SAME filters is the stronger reading of what
        these tests exist for, because a predicate present in only some slices
        is a predicate that leaks.
        """
        assert self.queries, "no query was issued"
        distinct = {self._filters_of(q) for q, _ in self.queries}
        assert len(distinct) == 1, (
            f"{len(self.queries)} statements with {len(distinct)} different "
            f"filters — one of them is missing a predicate: {sorted(distinct)}")
        return self.queries[0][0]

    @property
    def params(self) -> dict:
        assert self.queries, "no query was issued"
        return self.queries[0][1]

    def where_of(self, sql: str) -> str:
        """The WHERE clause, whitespace-flattened, for substring assertions."""
        m = re.search(r"WHERE(.*?)(ORDER BY|GROUP BY|LIMIT|$)", sql, re.S)
        return " ".join((m.group(1) if m else "").split())


class FakeEmbedder:
    def __init__(self):
        self.text_calls: list[str] = []
        self.image_calls = 0

    def embed_text(self, q):
        self.text_calls.append(q)
        return SimpleNamespace(tolist=lambda: [0.1, 0.2, 0.3])

    def embed_images(self, imgs):
        self.image_calls += 1
        return [SimpleNamespace(tolist=lambda: [0.4, 0.5, 0.6])]


class FakePool:
    """Stands in for ModelPool. `embedder=None` is an encoder that will not load."""

    def __init__(self, embedder=None, possible=True, plates=True):
        self._embedder = FakeEmbedder() if embedder is None and possible else embedder
        self.embedder_possible = possible
        self.plates_available = plates
        self.acquires = 0

    def acquire_embedder(self):
        self.acquires += 1
        return self._embedder


def service(store=None, pool=None, **pool_kw):
    return QueryService(store or RecordingStore(), pool or FakePool(**pool_kw))


def person_row(**over):
    row = {
        "id": 1, "sensor_id": "gate-a1b2", "ts": datetime(2026, 9, 10, 14, 30, tzinfo=UTC),
        "tracker_id": 7, "confidence": 0.9, "frame_number": 100, "pad_index": 0,
        "bbox": [1, 2, 3, 4], "score": 0.8,
        # Selected since migration 005: the card shows the whole frame when one
        # was kept, and falls back to the crop when it was not.
        "has_frame": False,
    }
    row.update(over)
    return row


# ── 1. The camera scope: an empty list is a filter, not the absence of one ──

class TestCameraScoping:
    """The single most dangerous thing in this module.

    `sensor_ids=[]` means "this VMS has no searchable cameras" — a real answer
    that must return nothing. Read as "no filter", it returns the ENTIRE index.
    The guard is `elif sensor_ids is not None`, and the difference between that
    and `elif sensor_ids` is invisible in every test that passes a non-empty
    list, which is every test anyone writes by habit.
    """

    def test_an_empty_camera_list_still_filters(self):
        st = RecordingStore()
        service(st).search_people(query="a man", top_k=10, score_threshold=0.0,
                                  sensor_ids=[])
        where = st.where_of(st.sql)
        assert "sensor_id = ANY" in where, (
            "an empty sensor_ids list produced an UNFILTERED query — it would "
            "return the whole index to a VMS that has no searchable cameras"
        )
        assert st.params["sensor_ids"] == []

    def test_none_means_no_camera_filter(self):
        st = RecordingStore()
        service(st).search_people(query="a man", top_k=10, score_threshold=0.0,
                                  sensor_ids=None)
        assert "sensor_id" not in st.where_of(st.sql)

    def test_a_populated_list_filters_on_it(self):
        st = RecordingStore()
        service(st).search_people(query="a man", top_k=10, score_threshold=0.0,
                                  sensor_ids=["a", "b"])
        assert "sensor_id = ANY" in st.where_of(st.sql)
        assert st.params["sensor_ids"] == ["a", "b"]

    def test_a_single_sensor_id_wins_over_the_list(self):
        st = RecordingStore()
        service(st).search_people(query="a man", top_k=10, score_threshold=0.0,
                                  sensor_id="only-this", sensor_ids=["a", "b"])
        where = st.where_of(st.sql)
        assert "sensor_id = %(sensor_id)s" in where
        assert "ANY" not in where

    def test_the_filter_is_on_the_id_column_never_a_name(self):
        # The retiring service resolved camera NAMES against its own table and
        # ran the query unfiltered when it did not recognise one.
        st = RecordingStore()
        service(st).search_people(query="a man", top_k=10, score_threshold=0.0,
                                  sensor_ids=["gate-a1b2"])
        assert "camera_name" not in st.sql
        assert "sensor_id" in st.where_of(st.sql)

    @pytest.mark.parametrize("ids", [[], ["x"]])
    def test_plate_search_scopes_the_same_way(self, ids):
        st = RecordingStore()
        service(st).search_plates(plate="MH12AB1234", camera_ids=ids)
        assert "camera_id = ANY" in st.where_of(st.sql)
        assert st.params["ids"] == ids

    @pytest.mark.parametrize("domain,col", [("person", "sensor_id"),
                                            ("vehicles", "camera_id")])
    def test_stats_scopes_on_the_right_column(self, domain, col):
        st = RecordingStore(rows=[{"n": 5}])
        service(st).stats(domain, camera_ids=[])
        assert f"{col} = ANY" in st.where_of(st.sql)
        assert st.params["ids"] == []

    def test_detections_scopes_every_query_it_issues(self):
        st = RecordingStore(rows=[])
        service(st).detections(since_ms=0, limit=10, tz_offset_min=0, camera_ids=[])
        assert st.queries, "detections issued no queries at all"
        for sql, params in st.queries:
            assert "= ANY(%(ids)s)" in st.where_of(sql), (
                f"an unscoped statement in the detections feed:\n{sql}"
            )
            assert params["ids"] == []


# ── 2. Expired rows must never answer a search ─────────────────────────────

class TestRetentionIsEnforcedOnRead:
    """`expires_at > now()` on every read is defence in depth for a sweep that
    is pending, disabled, behind, or failing. Without it a stalled sweep is
    invisible: the rows keep answering searches and the only symptom is disk.
    """

    def _all_reads(self, fn):
        st = RecordingStore(rows=[])
        fn(service(st))
        assert st.queries, "no query was issued"
        return st, [(sql, st.where_of(sql)) for sql, _ in st.queries]

    def test_people_search_excludes_expired(self):
        _, reads = self._all_reads(
            lambda s: s.search_people(query="x", top_k=5, score_threshold=0.0))
        assert all("expires_at > now()" in w for _, w in reads)

    def test_vehicle_search_excludes_expired(self):
        _, reads = self._all_reads(
            lambda s: s.search_vehicles(query="x", top_k=5, score_threshold=0.0))
        assert all("expires_at > now()" in w for _, w in reads)

    def test_plate_search_excludes_expired(self):
        _, reads = self._all_reads(
            lambda s: s.search_plates(plate="AB12", camera_ids=None))
        assert all("expires_at > now()" in w for _, w in reads)

    def test_stats_excludes_expired(self):
        st = RecordingStore(rows=[{"n": 1}])
        service(st).stats("person", camera_ids=None)
        assert "expires_at > now()" in st.where_of(st.sql)

    def test_crop_lookup_excludes_expired(self):
        st = RecordingStore(rows=[{"crop_path": "/x.jpg"}])
        service(st).crop_path("person", "12")
        assert "expires_at > now()" in st.where_of(st.sql)

    def test_every_detections_query_excludes_expired(self):
        st = RecordingStore(rows=[])
        service(st).detections(since_ms=0, limit=5, tz_offset_min=0, camera_ids=None)
        for sql, _ in st.queries:
            assert "expires_at > now()" in st.where_of(sql), (
                f"an expired-row leak in the detections feed:\n{sql}"
            )


# ── 3. "No results" must never be a lie ────────────────────────────────────

class TestEncoderOutageIsNotAnEmptyResult:
    """An operator reading "no results" cannot tell it from "this person was
    never here". When the encoder cannot be had there is no honest answer, so
    the module raises instead of returning [].
    """

    def test_text_search_raises_when_the_encoder_will_not_load(self):
        st = RecordingStore()
        svc = service(st, pool=FakePool(embedder=None, possible=False))
        with pytest.raises(EncoderUnavailable):
            svc.search_people(query="a man in red", top_k=5, score_threshold=0.0)
        assert st.queries == [], "a failed embed must not still run a query"

    def test_image_search_raises_when_the_encoder_will_not_load(self):
        svc = service(pool=FakePool(embedder=None, possible=False))
        with pytest.raises(EncoderUnavailable):
            svc.search_people_by_image(image=b"\x89PNG\r\n", top_k=5,
                                       score_threshold=0.0)

    def test_vehicle_search_raises_too(self):
        svc = service(pool=FakePool(embedder=None, possible=False))
        with pytest.raises(EncoderUnavailable):
            svc.search_vehicles(query="white van", top_k=5, score_threshold=0.0)

    def test_undecodable_image_is_a_value_error_not_an_outage(self):
        # The caller's 400, not a 503: distinguishing them is why _embed_image
        # catches the decode separately.
        svc = service()
        with pytest.raises(ValueError):
            svc.search_people_by_image(image=b"definitely not an image",
                                       top_k=5, score_threshold=0.0)

    def test_plate_lookup_works_with_no_encoder_at_all(self):
        # The most precise identifier the product has must keep working on a
        # box where the encoder failed to load.
        st = RecordingStore(rows=[])
        svc = service(st, pool=FakePool(embedder=None, possible=False))
        out = svc.search_plates(plate="MH12AB1234", camera_ids=None)
        assert out["scoped"] is True
        assert st.queries, "plate lookup should still have queried"


# ── 4. The scoped flag the VMS refuses a response without ──────────────────

class TestScopedFlag:
    def test_stats_is_always_scoped(self):
        st = RecordingStore(rows=[{"n": 0}])
        assert service(st).stats("person", camera_ids=None)["scoped"] is True

    def test_detections_is_always_scoped(self):
        st = RecordingStore(rows=[])
        out = service(st).detections(since_ms=0, limit=5, tz_offset_min=0,
                                     camera_ids=None)
        assert out["scoped"] is True

    def test_plates_are_scoped_even_when_the_query_was_empty(self):
        out = service().search_plates(plate="   ", camera_ids=None)
        assert out["scoped"] is True
        assert out["plates"] == []
        assert out["reason"] == "empty plate query"


# ── 5. Ranking, thresholds and clamping ────────────────────────────────────

class TestRankingAndLimits:
    def test_hits_below_the_threshold_are_dropped(self):
        st = RecordingStore(rows=[person_row(id=1, score=0.9),
                                  person_row(id=2, score=0.4)])
        out = service(st).search_people(query="x", top_k=10, score_threshold=0.5)
        assert [h["id"] for h in out] == ["1"]

    def test_a_hit_exactly_on_the_threshold_is_kept(self):
        st = RecordingStore(rows=[person_row(score=0.5)])
        assert len(service(st).search_people(
            query="x", top_k=10, score_threshold=0.5)) == 1

    def test_a_zero_threshold_keeps_everything_the_query_still_matches(self):
        """Rows best-first, as the SQL's ORDER BY guarantees.

        A zero threshold removes the OPERATOR's floor; it does not remove the
        relevance bar underneath it, which keeps what scores within 0.05 of the
        query's own best (index/queries.py _RELEVANCE_MARGIN). These two are
        0.04 apart, so both are matches and both are kept.
        """
        # Distinct tracker ids: one result per OBJECT, so two observations of
        # the same track would collapse to a single card whatever they scored.
        st = RecordingStore(rows=[person_row(id=2, score=0.99, tracker_id=2),
                                  person_row(id=1, score=0.95, tracker_id=1)])
        assert len(service(st).search_people(
            query="x", top_k=10, score_threshold=0.0)) == 2

    def test_a_zero_threshold_does_not_admit_a_non_match(self):
        """The other half of the same rule, and the reason the page stopped
        filling with generic people: 0.01 against a best of 0.99 is the index
        returning its nearest neighbour, not an answer."""
        st = RecordingStore(rows=[person_row(id=2, score=0.99, tracker_id=2),
                                  person_row(id=1, score=0.01, tracker_id=1)])
        assert [h["id"] for h in service(st).search_people(
            query="x", top_k=10, score_threshold=0.0)] == ["2"]

    @pytest.mark.parametrize("top_k", [0, -5])
    def test_a_non_positive_top_k_still_asks_for_at_least_one_row(self, top_k):
        # LIMIT 0 returns nothing and LIMIT -1 is a syntax error; either turns
        # a bad request into "nothing matched".
        st = RecordingStore()
        service(st).search_people(query="x", top_k=top_k, score_threshold=0.0)
        # Every statement the search issues, not just the first: the reference
        # read carries a constant limit and would pass this on its own.
        limits = [p["limit"] for _, p in st.queries if "limit" in p]
        assert limits and all(n >= 1 for n in limits)

    def test_the_plate_limit_is_clamped_to_a_sane_ceiling(self):
        st = RecordingStore(rows=[])
        service(st).search_plates(plate="AB", camera_ids=None, limit=999_999)
        assert st.params["limit"] == 2000

    def test_the_plate_limit_is_clamped_from_below(self):
        st = RecordingStore(rows=[])
        service(st).search_plates(plate="AB", camera_ids=None, limit=0)
        assert st.params["limit"] == 1


# ── 6. Timestamps: two formats, two consumers ──────────────────────────────

class TestTimestampFormats:
    """Search hits are ISO-8601; /detections is epoch MILLISECONDS because the
    dashboard does `new Date(ts)`. Swapping them renders 1970 or NaN.
    """

    def test_a_search_hit_carries_an_iso_timestamp(self):
        st = RecordingStore(rows=[person_row(
            ts=datetime(2026, 9, 10, 14, 30, 5, tzinfo=UTC))])
        hit = service(st).search_people(query="x", top_k=5, score_threshold=0.0)[0]
        assert hit["timestamp"] == "2026-09-10T14:30:05Z"

    def test_a_non_utc_hit_is_normalised_to_utc(self):
        ts = datetime(2026, 9, 10, 20, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        st = RecordingStore(rows=[person_row(ts=ts)])
        hit = service(st).search_people(query="x", top_k=5, score_threshold=0.0)[0]
        assert hit["timestamp"] == "2026-09-10T14:30:00Z"

    def test_the_detections_feed_carries_epoch_milliseconds(self):
        ts = datetime(2026, 9, 10, 14, 30, tzinfo=UTC)
        st = RecordingStore(by_match={
            "ORDER BY ts DESC LIMIT %(limit)s": [
                {"id": 1, "camera": "gate", "type": "person", "ts": ts,
                 "plate": None, "tracker_id": None,
                 "bbox": None, "has_frame": False, "frame_boxes": None},
            ],
        }, rows=[])
        out = service(st).detections(since_ms=0, limit=5, tz_offset_min=0,
                                     camera_ids=None)
        assert out["recent"][0]["timestamp"] == int(ts.timestamp() * 1000)
        assert out["recent"][0]["timestamp"] > 1_000_000_000_000  # ms, not seconds

    def test_the_detections_feed_carries_the_tracker_id(self):
        """The dashboard's "Tracker" line and its copy button read this field.
        Every other fixture in this file sets it to None, so dropping it from
        the response would pass them all and show "N/A" on every card."""
        ts = datetime(2026, 9, 10, 14, 30, tzinfo=UTC)
        st = RecordingStore(by_match={
            "ORDER BY ts DESC LIMIT %(limit)s": [
                {"id": 1, "camera": "gate", "type": "person", "ts": ts,
                 "plate": None, "tracker_id": 4217,
                 "bbox": None, "has_frame": False, "frame_boxes": None},
            ],
        }, rows=[])
        out = service(st).detections(since_ms=0, limit=5, tz_offset_min=0,
                                     camera_ids=None)
        assert out["recent"]
        assert all(r["tracker_id"] == 4217 for r in out["recent"])

    @pytest.mark.parametrize("bad", ["", "not-a-date", "2026-13-45"])
    def test_an_unparseable_time_bound_is_ignored_rather_than_fatal(self, bad):
        st = RecordingStore()
        service(st).search_people(query="x", top_k=5, score_threshold=0.0,
                                  time_from=bad)
        assert "ts >=" not in st.where_of(st.sql)

    def test_a_z_suffixed_time_bound_is_understood(self):
        st = RecordingStore()
        service(st).search_people(query="x", top_k=5, score_threshold=0.0,
                                  time_from="2026-09-10T00:00:00Z")
        assert "ts >= %(frm)s" in st.where_of(st.sql)
        assert st.params["frm"].tzinfo is not None


# ── 7. The detections summary must stay whole ──────────────────────────────

class TestDomainsFilterTheFeedOnly:
    """`domains` narrows the FEED, never the summary. A filter that also moved
    the totals would let an operator read "1,906 vehicles" as the whole site.
    """

    def _store(self):
        ts = datetime(2026, 9, 10, 14, 0, tzinfo=UTC)
        return RecordingStore(by_match={
            "GROUP BY 1, 2, 4": [{"camera": "gate", "type": "person", "n": 10,
                                  "hour": "14:00"}],
            "ORDER BY ts DESC LIMIT %(limit)s": [
                {"id": 1, "camera": "gate", "type": "person", "ts": ts,
                 "plate": None, "tracker_id": None,
                 "bbox": None, "has_frame": False, "frame_boxes": None},
            ],
        }, rows=[])

    def test_the_summary_totals_ignore_the_domain_filter(self):
        both = service(self._store()).detections(
            since_ms=0, limit=10, tz_offset_min=0, camera_ids=None)
        filtered = service(self._store()).detections(
            since_ms=0, limit=10, tz_offset_min=0, camera_ids=None,
            domains=["vehicles"])
        assert filtered["summary"]["total"] == both["summary"]["total"]
        assert filtered["summary"]["by_domain"] == both["summary"]["by_domain"]

    def test_the_feed_honours_the_domain_filter(self):
        out = service(self._store()).detections(
            since_ms=0, limit=10, tz_offset_min=0, camera_ids=None,
            domains=["vehicles"])
        assert all(r["domain"] == "vehicles" for r in out["recent"])

    def test_no_domain_filter_leaves_the_feed_alone(self):
        out = service(self._store()).detections(
            since_ms=0, limit=10, tz_offset_min=0, camera_ids=None)
        assert any(r["domain"] == "person" for r in out["recent"])


class TestDetectionsShape:
    def test_the_hour_chart_always_has_all_twenty_four_bars(self):
        st = RecordingStore(rows=[])
        out = service(st).detections(since_ms=0, limit=5, tz_offset_min=0,
                                     camera_ids=None)
        hours = [b["hour"] for b in out["summary"]["by_hour"]]
        assert len(hours) == 24
        assert hours[0] == "00:00" and hours[-1] == "23:00"

    def test_the_viewer_timezone_shifts_the_buckets(self):
        st = RecordingStore(rows=[])
        service(st).detections(since_ms=0, limit=5, tz_offset_min=330,
                               camera_ids=None)
        shifts = [p.get("shift") for _, p in st.queries if "shift" in p]
        assert shifts and all(s == timedelta(minutes=330) for s in shifts)

    def test_vehicles_are_reported_under_both_spellings(self):
        # The retiring index spelled the key "vehicle" while the route was
        # "/vehicles"; the dashboard reads the singular. Emitting only one
        # silently zeroes a panel on cutover.
        st = RecordingStore(by_match={
            "GROUP BY 1, 2, 4": [{"camera": "gate", "type": "car", "n": 4,
                                  "hour": "09:00"}],
        }, rows=[])
        out = service(st).detections(since_ms=0, limit=5, tz_offset_min=0,
                                     camera_ids=None)
        by_domain = out["summary"]["by_domain"]
        assert by_domain.get("vehicle") == by_domain.get("vehicles")

    def test_plate_reads_are_not_folded_into_the_type_counts(self):
        # A vehicle is indexed whether or not its plate was read, so counting
        # plates inside by_type would double-count.
        st = RecordingStore(by_match={
            "count(DISTINCT plate)": [{"n": 41, "d": 12}],
            "GROUP BY 1, 2, 4": [{"camera": "gate", "type": "car", "n": 380,
                                  "hour": "09:00"}],
        }, rows=[])
        out = service(st).detections(since_ms=0, limit=5, tz_offset_min=0,
                                     camera_ids=None)
        s = out["summary"]
        assert s["plates_read"] == 41 and s["distinct_plates"] == 12
        assert "plate" not in s["by_type"]

    def test_a_negative_since_is_clamped_to_the_epoch(self):
        st = RecordingStore(rows=[])
        service(st).detections(since_ms=-5000, limit=5, tz_offset_min=0,
                               camera_ids=None)
        since = next(p["since"] for _, p in st.queries if "since" in p)
        assert since == datetime.fromtimestamp(0, tz=UTC)

    def test_the_feed_is_capped_at_the_requested_limit(self):
        ts = datetime(2026, 9, 10, 14, 0, tzinfo=UTC)
        many = [{"id": i, "camera": "gate", "type": "person",
                 "ts": ts - timedelta(seconds=i), "plate": None, "tracker_id": None,
                 "bbox": None, "has_frame": False, "frame_boxes": None}
                for i in range(50)]
        st = RecordingStore(by_match={"ORDER BY ts DESC LIMIT %(limit)s": many},
                            rows=[])
        out = service(st).detections(since_ms=0, limit=3, tz_offset_min=0,
                                     camera_ids=None)
        assert len(out["recent"]) == 3

    def test_the_feed_is_newest_first_across_both_domains(self):
        old = datetime(2026, 9, 10, 10, 0, tzinfo=UTC)
        new = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)
        st = RecordingStore(by_match={"ORDER BY ts DESC LIMIT %(limit)s": [
            {"id": 1, "camera": "a", "type": "person", "ts": old,
             "plate": None, "tracker_id": None,
                 "bbox": None, "has_frame": False, "frame_boxes": None},
            {"id": 2, "camera": "b", "type": "person", "ts": new,
             "plate": None, "tracker_id": None,
                 "bbox": None, "has_frame": False, "frame_boxes": None},
        ]}, rows=[])
        out = service(st).detections(since_ms=0, limit=10, tz_offset_min=0,
                                     camera_ids=None)
        stamps = [r["timestamp"] for r in out["recent"]]
        assert stamps == sorted(stamps, reverse=True)


# ── 8. Plate grouping and normalisation ────────────────────────────────────

class TestPlateSearch:
    def _rows(self):
        return [
            {"id": 1, "plate": "MH12AB1234", "camera_id": "gate",
             "ts": datetime(2026, 9, 10, 12, 0, tzinfo=UTC), "vehicle_type": "car",
             "color": "white", "confidence": 0.9, "bbox": [1, 2, 3, 4]},
            {"id": 2, "plate": "MH12AB1234", "camera_id": "exit",
             "ts": datetime(2026, 9, 10, 9, 0, tzinfo=UTC), "vehicle_type": "car",
             "color": "white", "confidence": 0.8, "bbox": None},
        ]

    def test_a_typed_plate_is_normalised_before_matching(self):
        # Spacing and punctuation vary by region, by operator and by whoever
        # typed the query, and none of it means anything.
        st = RecordingStore(rows=[])
        service(st).search_plates(plate=" mh-12 ab 1234 ", camera_ids=None)
        assert st.params["needle"] == "%MH12AB1234%"

    def test_an_all_punctuation_query_is_refused_rather_than_matching_everything(self):
        # normalise("---") is "", and "%%" would match every plate in the index.
        st = RecordingStore(rows=[])
        out = service(st).search_plates(plate="---", camera_ids=None)
        assert out["plates"] == []
        assert st.queries == [], "an empty needle must not reach the database"

    def test_sightings_are_grouped_per_plate(self):
        st = RecordingStore(rows=self._rows())
        out = service(st).search_plates(plate="MH12", camera_ids=None)
        assert len(out["plates"]) == 1
        assert out["plates"][0]["count"] == 2

    def test_first_and_last_seen_bracket_the_sightings(self):
        st = RecordingStore(rows=self._rows())
        p = service(st).search_plates(plate="MH12", camera_ids=None)["plates"][0]
        assert p["last_seen"] == "2026-09-10T12:00:00Z"
        assert p["first_seen"] == "2026-09-10T09:00:00Z"

    def test_the_cameras_a_plate_was_seen_on_are_deduplicated(self):
        st = RecordingStore(rows=self._rows())
        p = service(st).search_plates(plate="MH12", camera_ids=None)["plates"][0]
        assert p["cameras"] == ["exit", "gate"]

    def test_plates_active_is_reported_so_empty_is_not_read_as_never_seen(self):
        st = RecordingStore(rows=[])
        assert service(st, pool=FakePool(plates=False)).search_plates(
            plate="AB12", camera_ids=None)["plates_active"] is False


# ── 9. crop_path: an id from a URL is not to be trusted ────────────────────

class TestCropPath:
    @pytest.mark.parametrize("bad", ["abc", "1; DROP TABLE search_persons",
                                     "", None, "1.5", "0x10"])
    def test_a_non_integer_id_returns_nothing_and_queries_nothing(self, bad):
        st = RecordingStore(rows=[{"crop_path": "/should-not-happen.jpg"}])
        assert service(st).crop_path("person", bad) is None
        assert st.queries == [], "a malformed id reached the database"

    def test_an_integer_id_is_passed_as_a_bound_parameter(self):
        st = RecordingStore(rows=[{"crop_path": "/crops/1.jpg"}])
        assert service(st).crop_path("person", "42") == "/crops/1.jpg"
        assert st.params["id"] == 42
        assert "42" not in st.sql, "the id was interpolated into the SQL text"

    def test_a_missing_row_is_none_rather_than_an_error(self):
        st = RecordingStore(rows=[])
        assert service(st).crop_path("person", "42") is None

    @pytest.mark.parametrize("domain,table", [("person", "search_persons"),
                                              ("vehicles", "search_vehicles")])
    def test_the_domain_picks_the_table(self, domain, table):
        st = RecordingStore(rows=[])
        service(st).crop_path(domain, "1")
        assert table in st.sql


# ── 10. Availability reporting ─────────────────────────────────────────────

class TestAvailability:
    def test_a_hibernating_encoder_still_reports_search_as_possible(self):
        # Reporting a hibernating service as unavailable makes /health say
        # search is broken on every quiet deployment — the false alarm that
        # trains operators to ignore it.
        assert service(pool=FakePool(possible=True)).available is True

    def test_an_impossible_encoder_reports_unavailable(self):
        assert service(pool=FakePool(embedder=None, possible=False)).available is False

    def test_plates_active_follows_the_deployment_configuration(self):
        assert service(pool=FakePool(plates=False)).plates_active is False
        assert service(pool=FakePool(plates=True)).plates_active is True


# ── 11. Dashboard page sizes stay page-sized ───────────────────────────────

def test_the_plate_panels_ask_for_their_documented_page_sizes():
    st = RecordingStore(rows=[])
    service(st).detections(since_ms=0, limit=5, tz_offset_min=0, camera_ids=None)
    limits = {k: v for _, p in st.queries for k, v in p.items()
              if k in ("plimit", "tlimit")}
    assert limits.get("plimit") == _RECENT_PLATES
    assert limits.get("tlimit") == _TOP_PLATES
