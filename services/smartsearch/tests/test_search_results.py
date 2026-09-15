"""What a page of search results is, once the vector search has ranked the rows.

Four decisions live between "nearest crops" and "what the operator sees", and
all four are presentation — nothing here changes what is stored:

  * RELEVANCE BEFORE RECENCY. The vector search always returns its nearest
    crops, however far away they are, so "nearest" has to be turned into
    "matches" before recency may reorder anything. The bar is relative to what
    the query itself can achieve, because 0.26 is the best score any crop gets
    for "person" and a poor one for "man in a red shirt".
  * A RECENT WINDOW, WIDENED ONLY AS NEEDED. A VMS search is nearly always about
    what just happened, and ranking cannot deliver that: a top-24 page for
    "person" spans 0.2735 to 0.2620 with gaps as small as 0.00006, so yesterday
    outranks a minute ago by noise. The window decides which matches are on the
    page; CLIP still decides which of them match at all.
  * ONE RESULT PER TRACKED OBJECT. A long stay is several honest rows; a page of
    the same person eight times is not an answer to "find this person".
  * ONE FIELD ORDERS THE PAGE — the time it happened or how well it matched,
    whichever the operator picked, with the other never consulted. Newest first
    by default, the same page twice for the same query, and no ordering can
    change WHICH results are on the page.

Run: python3 -m pytest tests -q
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from index.queries import (Order, _REFERENCE_ROWS, _WINDOW_STEPS,   # noqa: E402
                           _candidate_limit, _one_per_object, _recent_first,
                           classes_named_in)

def now() -> datetime:
    """Read per call, never captured at import. _recent_first measures its
    window edges from the wall clock, so a module-level constant would drift by
    however long the rest of the suite took to run before reaching these tests
    — which is exactly how these two first failed."""
    return datetime.now(timezone.utc)


def row(rid: int, score: float, *, tracker: str | None = "cam-a:n.0:1",
        age_s: float = 0) -> dict:
    """One indexed observation, `age_s` seconds old."""
    return {"id": rid, "score": score, "tracker_id": tracker,
            "ts": now() - timedelta(seconds=age_s)}


def ids(rows) -> list[int]:
    return [r["id"] for r in rows]


class Index:
    """A fake index: rows served best-first out of whatever slice is asked for,
    recording every call so the window walk itself can be checked."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[tuple] = []

    def __call__(self, since, until, limit):
        self.calls.append((since, until, limit))
        got = [r for r in self.rows
               if (since is None or r["ts"] >= since)
               and (until is None or r["ts"] < until)]
        got.sort(key=lambda r: (-r["score"], -r["id"]))     # as the SQL orders
        return got[:limit]

    def ages(self) -> list[float | None]:
        """How far back each call reached, in seconds. None = no lower bound."""
        ref = now()
        return [None if since is None else round((ref - since).total_seconds())
                for since, _until, _limit in self.calls]


def reached(idx: "Index", *expected: float | None, slack: float = 5) -> bool:
    """Did the walk reach back exactly this far, step by step? Compared with a
    few seconds of slack: the edges are measured from the clock inside
    _recent_first, a moment after these rows were built."""
    got = idx.ages()
    if len(got) != len(expected):
        return False
    return all((a is None and e is None) or
               (a is not None and e is not None and abs(a - e) <= slack)
               for a, e in zip(got, expected))


# ── one result per object ───────────────────────────────────────────────────
def test_repeats_of_one_object_collapse_to_its_best_result():
    """Eight observations of one person are one answer, not eight."""
    rows = [row(i, 0.30 - i * 0.001, age_s=i * 10) for i in range(8)]
    out = _one_per_object(rows, top_k=24)
    assert ids(out) == [0], "the best-ranked observation should represent the object"
    assert out[0]["sightings"] == 8


def test_different_objects_are_not_merged():
    rows = [row(1, 0.30, tracker="cam-a:n.0:1", age_s=10),
            row(2, 0.29, tracker="cam-a:n.0:2", age_s=20),
            row(3, 0.28, tracker="cam-b:n.0:1", age_s=30)]
    out = _one_per_object(rows, top_k=24)
    assert ids(out) == [1, 2, 3]
    assert [r["sightings"] for r in out] == [1, 1, 1]


def test_rows_from_before_tracking_each_stand_for_themselves():
    """21,464 person rows have no tracker id. Collapsing them together would
    merge strangers; each is its own object instead."""
    rows = [row(1, 0.30, tracker=None, age_s=10), row(2, 0.29, tracker=None, age_s=20)]
    assert ids(_one_per_object(rows, top_k=24)) == [1, 2]


def test_the_page_is_top_k_objects_not_top_k_rows():
    """A page of 3 must be 3 different objects, even when the nearest 6 rows are
    3 objects twice over."""
    rows = [row(1, 0.30, tracker="a", age_s=10), row(2, 0.29, tracker="a", age_s=15),
            row(3, 0.28, tracker="b", age_s=20), row(4, 0.27, tracker="b", age_s=25),
            row(5, 0.26, tracker="c", age_s=30), row(6, 0.25, tracker="c", age_s=35)]
    out = _one_per_object(rows, top_k=3)
    assert ids(out) == [1, 3, 5]
    assert [r["sightings"] for r in out] == [2, 2, 2]


# ── the order ───────────────────────────────────────────────────────────────
def test_the_default_page_reads_newest_first():
    """Time, descending: what a VMS page has always been. The scores run the
    other way on purpose — if similarity had any say here, this would reverse."""
    rows = [row(1, 0.34, tracker="a", age_s=3600),
            row(2, 0.28, tracker="b", age_s=60),
            row(3, 0.31, tracker="c", age_s=600)]
    assert ids(_one_per_object(rows, top_k=24)) == [2, 3, 1]


def test_two_rows_at_the_same_instant_never_swap_places():
    """The same query over the same data has to produce the same page. The
    scores differ and must not be what separates them — id does."""
    a = row(11, 0.2700, tracker="a")
    b = {**row(12, 0.2600, tracker="b"), "ts": a["ts"]}
    assert ids(_one_per_object([a, b], top_k=24)) == [12, 11]
    assert ids(_one_per_object([b, a], top_k=24)) == [12, 11]


# ── the recent window ───────────────────────────────────────────────────────
def test_a_busy_search_never_looks_past_the_first_window():
    """The ordinary daytime case: the last quarter of an hour fills the page, so
    nothing older is read at all."""
    idx = Index([row(i, 0.30, tracker=f"t{i}", age_s=i * 10) for i in range(40)])
    out = _recent_first(idx, top_k=12, score_threshold=0.05, windowed=True)
    # The reference read, then one window. Nothing older is touched.
    assert len(idx.calls) == 2, "it widened the window with a full page in hand"
    assert reached(idx, None, _WINDOW_STEPS[0])
    assert len(out) == 12
    assert out[0]["ts"] > out[-1]["ts"], "the page is not newest first"


def test_a_quiet_camera_reaches_back_until_the_page_fills():
    """3am on one camera: two matches in the last quarter hour, the rest hours
    old. The page still fills — it just reaches further to do it."""
    rows = [row(1, 0.30, tracker="a", age_s=60), row(2, 0.30, tracker="b", age_s=120)]
    rows += [row(10 + i, 0.30, tracker=f"old{i}", age_s=5 * 3600 + i) for i in range(6)]
    idx = Index(rows)
    out = _recent_first(idx, top_k=5, score_threshold=0.05, windowed=True)
    assert reached(idx, None, 900, 1800, 3600, 10_800, 21_600), "unexpected widening"
    assert len(out) == 5
    # Newest first across the whole widened window, not window by window.
    assert ids(out)[:2] == [1, 2]


def test_each_pass_reads_only_the_slice_it_adds():
    """Ten steps must cost one scan of the time range between them, not ten
    overlapping ones — so every pass after the first is bounded above by where
    the previous pass stopped."""
    idx = Index([row(1, 0.30, tracker="a", age_s=20 * 86_400)])
    _recent_first(idx, top_k=5, score_threshold=0.05, windowed=True)
    walk = idx.calls[1:]               # [0] is the reference, which spans all time
    lower = [since for since, _u, _l in walk]
    upper = [until for _s, until, _l in walk]
    assert upper[0] is None, "the newest slice must have no upper edge"
    assert upper[1:] == lower[:-1], "slices overlap or leave a gap"


def test_the_last_step_reaches_everything_the_index_still_holds():
    """Nothing may be unreachable: a match older than every step must still be
    found once the window has run out of steps."""
    idx = Index([row(1, 0.30, tracker="a", age_s=400 * 86_400)])
    out = _recent_first(idx, top_k=5, score_threshold=0.05, windowed=True)
    assert idx.ages()[-1] is None, "the widest pass is still bounded"
    assert ids(out) == [1]


def test_an_empty_index_gives_an_empty_page_rather_than_a_loop():
    idx = Index([])
    assert _recent_first(idx, top_k=12, score_threshold=0.05, windowed=True) == []
    assert len(idx.calls) == len(_WINDOW_STEPS) + 1


def test_the_threshold_still_decides_what_counts_as_a_match():
    """A weak row is not a match, so it neither shows nor stops the widening."""
    idx = Index([row(1, 0.04, tracker="a", age_s=60),          # below threshold
                 row(2, 0.08, tracker="b", age_s=4 * 3600)])
    out = _recent_first(idx, top_k=1, score_threshold=0.05, windowed=True)
    assert ids(out) == [2]
    assert len(idx.calls) > 1, "a below-threshold row was counted as a result"


def test_repeats_do_not_fool_the_window_into_stopping_early():
    """Enough distinct OBJECTS, not rows: eight sightings of one person are one
    result, and the window must keep widening to find the others."""
    idx = Index([row(i, 0.30, tracker="a", age_s=i) for i in range(8)]
                + [row(50, 0.30, tracker="b", age_s=4 * 3600)])
    out = _recent_first(idx, top_k=2, score_threshold=0.05, windowed=True)
    assert len(idx.calls) > 1
    assert [r["tracker_id"] for r in out] == ["a", "b"]


# ── an explicit range is the window ─────────────────────────────────────────
def test_an_explicit_range_is_asked_for_once_and_never_widened():
    """The operator named the window. Widening it would return footage from
    outside the period they asked about, which is the one thing a time filter
    must never do."""
    idx = Index([row(1, 0.30, tracker="a", age_s=60)])
    out = _recent_first(idx, top_k=12, score_threshold=0.05, windowed=False)
    assert idx.calls == [(None, None, _candidate_limit(12))], \
        "an explicit range must not impose a window of its own"
    assert ids(out) == [1]


def test_an_explicit_range_still_reads_newest_first():
    # Both clear the relevance bar (0.03 apart); the scores are in the opposite
    # order to the ages, so this pins the ORDER inside a named range.
    idx = Index([row(1, 0.34, tracker="a", age_s=1800),
                 row(2, 0.31, tracker="b", age_s=120)])
    assert ids(_recent_first(idx, top_k=12, score_threshold=0.05, windowed=False)) == [2, 1]


# ── how much is fetched to make that possible ───────────────────────────────
def test_the_candidate_window_is_wider_than_the_page_but_bounded():
    assert _candidate_limit(24) == 96
    assert _candidate_limit(1) == 4
    # A caller asking for the maximum page must not turn into an unbounded scan.
    assert _candidate_limit(240) == 400


# ── relevance before recency ────────────────────────────────────────────────
def test_a_weak_recent_match_never_displaces_a_strong_older_one():
    """THE COMPLAINT THIS EXISTS FOR. A page for "man in a red shirt" filled up
    with generic people three seconds old at 0.22 while the actual red shirts,
    five minutes back at 0.33, were never reached."""
    idx = Index([row(i, 0.22, tracker=f"g{i}", age_s=i + 1) for i in range(20)]
                + [row(99, 0.33, tracker="red", age_s=300)])
    out = _recent_first(idx, top_k=12, score_threshold=0.05, windowed=True)
    assert ids(out) == [99], "generic people outranked the thing that was asked for"


def test_relevance_is_measured_against_the_query_not_a_fixed_floor():
    """0.26 is a fine "person" and a poor "man in a red shirt", so the same
    score has to survive one query and be dropped by the other. This is why the
    bar cannot be a constant, and why Minimum Score alone could not fix it."""
    broad = Index([row(1, 0.2735, tracker="a", age_s=300),
                   row(2, 0.2600, tracker="b", age_s=10)])
    assert len(_recent_first(broad, top_k=12, score_threshold=0.05, windowed=True)) == 2

    specific = Index([row(1, 0.3486, tracker="a", age_s=300),
                      row(2, 0.2600, tracker="b", age_s=10)])
    assert ids(_recent_first(specific, top_k=12, score_threshold=0.05, windowed=True)) == [1]


def test_the_operators_minimum_score_is_still_the_floor():
    """The bar only ever tightens. A query whose best match is itself weak must
    not drag everything under the operator's floor onto the page with it."""
    idx = Index([row(1, 0.16, tracker="a", age_s=10),
                 row(2, 0.19, tracker="b", age_s=20)])
    assert ids(_recent_first(idx, top_k=12, score_threshold=0.17, windowed=True)) == [2]


def test_a_short_page_beats_a_padded_one():
    """Two things match; asking for twelve returns two, having looked as far
    back as the index goes for the rest."""
    idx = Index([row(1, 0.34, tracker="a", age_s=60), row(2, 0.33, tracker="b", age_s=90)]
                + [row(10 + i, 0.20, tracker=f"w{i}", age_s=30 + i) for i in range(30)])
    out = _recent_first(idx, top_k=12, score_threshold=0.05, windowed=True)
    assert ids(out) == [1, 2]
    assert idx.ages()[-1] is None, "it gave up before looking everywhere"


def test_the_reference_is_read_once_and_spans_all_of_time():
    """One extra query per search, before the walk, unbounded in time — that is
    what makes the bar mean "the best this query can do", not "the best the
    last quarter of an hour happened to hold"."""
    idx = Index([row(1, 0.30, tracker="a", age_s=60)])
    _recent_first(idx, top_k=12, score_threshold=0.05, windowed=True)
    assert idx.calls[0] == (None, None, _REFERENCE_ROWS)
    assert all(since is not None for since, _u, _l in idx.calls[1:-1])


def test_an_explicit_range_gates_on_the_best_inside_it():
    """The operator named the period, so the best match INSIDE it is the scale.
    Still one query: the range is already ranked, and nothing outside it may be
    consulted even to set a bar."""
    idx = Index([row(1, 0.34, tracker="a", age_s=1800),
                 row(2, 0.22, tracker="b", age_s=60)])
    out = _recent_first(idx, top_k=12, score_threshold=0.05, windowed=False)
    assert ids(out) == [1]
    assert idx.calls == [(None, None, _candidate_limit(12))]


# ── what a collapsed result says about itself ───────────────────────────────
def veh(rid: int, score: float, *, label: str, conf: float,
        tracker: str = "cam-a:n.0:1", age_s: float = 0) -> dict:
    """A vehicle observation: the detector's class and how sure it was."""
    return {**row(rid, score, tracker=tracker, age_s=age_s),
            "vehicle_type": label, "confidence": conf}


def test_a_collapsed_result_reports_how_many_observations_it_stands_for():
    """It was counted and then thrown away: the page was collapsed twice, and
    the second pass reset every count to 1."""
    rows = [row(1, 0.34, tracker="a", age_s=10), row(2, 0.30, tracker="a", age_s=20),
            row(3, 0.28, tracker="b", age_s=15)]
    assert {r["tracker_id"]: r["sightings"] for r in _one_per_object(rows, top_k=24)} \
        == {"a": 2, "b": 1}


def test_the_best_matching_observation_represents_the_object():
    """Rows arrive from several time slices, so "the first one seen" is not the
    best one and is not even stable."""
    rows = [row(1, 0.30, tracker="a", age_s=10), row(2, 0.34, tracker="a", age_s=20)]
    assert ids(_one_per_object(rows, top_k=24)) == [2]


def test_the_class_comes_from_the_observations_the_detector_was_surest_of():
    """Tracker d34ae9.0:4917 was called a truck at confidence 0.39 off a 41x179
    px sliver clipped by the frame edge, while the same object seen properly was
    a motorcycle. The picture stays the best match; the class does not have to."""
    rows = [veh(1, 0.33, label="truck", conf=0.39, age_s=5),
            veh(2, 0.31, label="motorcycle", conf=0.85, age_s=6)]
    rep, = _one_per_object(rows, top_k=24)
    assert rep["id"] == 1, "the best-matching observation should still be shown"
    assert rep["vehicle_type"] == "motorcycle"
    assert rep["confidence"] == 0.85, "the confidence must describe the class beside it"


def test_the_class_vote_weighs_confidence_rather_than_counting_heads():
    """Two hesitant looks do not outvote one clear one."""
    rows = [veh(1, 0.33, label="car", conf=0.36, age_s=1),
            veh(2, 0.32, label="car", conf=0.35, age_s=2),
            veh(3, 0.31, label="bus", conf=0.90, age_s=3)]
    assert _one_per_object(rows, top_k=24)[0]["vehicle_type"] == "bus"


def test_a_single_observation_keeps_its_own_class():
    """Nothing to vote with, and inventing agreement would be worse than the
    detector's own answer."""
    rows = [veh(1, 0.33, label="truck", conf=0.39)]
    assert _one_per_object(rows, top_k=24)[0]["vehicle_type"] == "truck"


def test_a_person_result_has_no_class_to_vote_on():
    rows = [row(1, 0.34, tracker="a", age_s=1), row(2, 0.30, tracker="a", age_s=2)]
    rep, = _one_per_object(rows, top_k=24)
    assert "vehicle_type" not in rep and rep["sightings"] == 2


# ── the class the operator typed ────────────────────────────────────────────
def test_a_named_class_is_recognised():
    """A class in the query is a fact about the detector's label, not a hint to
    CLIP: "white car" and "white truck" look nearly identical to an embedding,
    which is how a card labelled truck answered a search for a car."""
    assert classes_named_in("white car") == ["car"]
    assert classes_named_in("red motorcycle") == ["motorcycle"]
    assert classes_named_in("blue truck") == ["truck"]
    assert classes_named_in("yellow bus") == ["bus"]


def test_plurals_are_the_same_class():
    assert classes_named_in("two red motorcycles") == ["motorcycle"]
    assert classes_named_in("buses at the gate") == ["bus"]
    assert classes_named_in("white cars") == ["car"]


def test_a_class_hiding_inside_another_word_is_not_a_class():
    """THE SUBSTRING TRAP. "person carrying a bag" contains "car" three letters
    in; matching it would silently restrict that search to cars and report the
    result as if it were the whole answer."""
    for q in ("person carrying a bag", "a man with a carton", "business entrance",
              "someone in a scarf", "by the carpark", "a busy road"):
        assert classes_named_in(q) == [], q


def test_a_description_with_no_class_constrains_nothing():
    """Only the four classes the detector can emit are classes. "van" and
    "sedan" never appear in vehicle_type, so treating them as classes would
    filter every row away and call it "no matches"."""
    for q in ("white vehicle", "white", "vehicle at the gate", "silver sedan",
              "white delivery van", "something at the barrier", ""):
        assert classes_named_in(q) == [], q


def test_two_named_classes_widen_rather_than_contradict():
    assert classes_named_in("a car or a truck") == ["car", "truck"]


def test_case_and_punctuation_do_not_hide_a_class():
    assert classes_named_in("RED MOTORCYCLE!") == ["motorcycle"]
    assert classes_named_in("white car, moving fast") == ["car"]


# ── the operator's ordering ─────────────────────────────────────────────────
TIME_NEWEST = Order.of("time", "desc")
TIME_OLDEST = Order.of("time", "asc")
BEST_FIRST = Order.of("confidence", "desc")
WEAKEST_FIRST = Order.of("confidence", "asc")


def test_time_newest_orders_by_timestamp_alone():
    """Scores ascending against ages ascending: any influence from similarity
    would show up as a different order."""
    rows = [row(1, 0.20, tracker="a", age_s=10),
            row(2, 0.30, tracker="b", age_s=100),
            row(3, 0.40, tracker="c", age_s=1000)]
    assert ids(_one_per_object(rows, 24, TIME_NEWEST)) == [1, 2, 3]


def test_time_oldest_orders_by_timestamp_alone():
    rows = [row(1, 0.40, tracker="a", age_s=10),
            row(2, 0.30, tracker="b", age_s=100),
            row(3, 0.20, tracker="c", age_s=1000)]
    assert ids(_one_per_object(rows, 24, TIME_OLDEST)) == [3, 2, 1]


def test_confidence_highest_orders_by_similarity_alone():
    """Ages run opposite to scores here, so time having any say would show."""
    rows = [row(1, 0.40, tracker="a", age_s=1000),
            row(2, 0.30, tracker="b", age_s=100),
            row(3, 0.20, tracker="c", age_s=10)]
    assert ids(_one_per_object(rows, 24, BEST_FIRST)) == [1, 2, 3]


def test_confidence_lowest_orders_by_similarity_alone():
    rows = [row(1, 0.20, tracker="a", age_s=1000),
            row(2, 0.30, tracker="b", age_s=100),
            row(3, 0.40, tracker="c", age_s=10)]
    assert ids(_one_per_object(rows, 24, WEAKEST_FIRST)) == [1, 2, 3]


def test_the_inactive_field_is_never_consulted():
    """NOT EVEN AS A TIE-BREAK, which is the whole point of the rewrite: a
    hidden second key is what made each control look broken in turn.

    Sorting by time, two rows sharing an instant are separated by id and not by
    which matched better. Sorting by the match, two rows scoring alike are
    separated by id and not by which is newer.
    """
    a = row(11, 0.20, tracker="a", age_s=50)
    same_instant = {**row(12, 0.40, tracker="b"), "ts": a["ts"]}
    assert ids(_one_per_object([a, same_instant], 24, TIME_NEWEST)) == [12, 11]
    assert ids(_one_per_object([a, same_instant], 24, TIME_OLDEST)) == [12, 11]

    c = row(21, 0.33, tracker="c", age_s=10)
    same_score = row(22, 0.33, tracker="d", age_s=9999)
    assert ids(_one_per_object([c, same_score], 24, BEST_FIRST)) == [22, 21]
    assert ids(_one_per_object([c, same_score], 24, WEAKEST_FIRST)) == [22, 21]


def test_the_mode_decides_what_the_direction_means():
    """One direction on the wire, two vocabularies: "desc" is newest in one
    mode and strongest in the other."""
    rows = [row(1, 0.40, tracker="a", age_s=1000), row(2, 0.20, tracker="b", age_s=10)]
    assert ids(_one_per_object(rows, 24, TIME_NEWEST)) == [2, 1]
    assert ids(_one_per_object(rows, 24, BEST_FIRST)) == [1, 2]


def test_ordering_never_readmits_a_weak_match():
    """Ordering arranges what a search found; it may not change what it found."""
    idx = Index([row(1, 0.34, tracker="a", age_s=300),
                 row(2, 0.22, tracker="b", age_s=10)])
    for order in (TIME_NEWEST, TIME_OLDEST, BEST_FIRST, WEAKEST_FIRST):
        out = _recent_first(idx, top_k=12, score_threshold=0.05,
                            windowed=False, order=order)
        assert ids(out) == [1], "an ordering brought a below-bar row onto the page"


def test_only_time_ascending_reaches_back_thirty_days():
    """The bound belongs to asking for the OLDEST thing, not to asking for the
    weakest one — _people_by_vector and search_vehicles read this flag."""
    assert TIME_OLDEST.oldest_first is True
    assert TIME_NEWEST.oldest_first is False
    assert WEAKEST_FIRST.oldest_first is False
    assert BEST_FIRST.oldest_first is False


def test_the_wire_values_map_to_an_order():
    assert Order.of("time", "desc") == Order(by="time", ascending=False)
    assert Order.of("confidence", "asc") == Order(by="confidence", ascending=True)
    assert Order.of() == Order(by="time", ascending=False)
    # Anything unexpected reads as the default: the API validates these, and a
    # page that renders beats a 500 over a stale query string.
    assert Order.of("nonsense", "nonsense") == Order()
