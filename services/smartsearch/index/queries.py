"""queries.py — the six endpoints the VMS actually calls.

The contract is not negotiable and it is not four endpoints. `smartsearch_client`
calls four; `routers/search.py` reaches past that client and calls two more
directly. Missing either of those does not fail loudly — it produces a results
grid with no thumbnails and an AI Detections dashboard that 502s.

Three rules the retiring service learned expensively, all enforced here:

  * `scoped: true` on stats and detections. The VMS refuses a response without
    it, because an unscoped total from a shared index is another site's data.
    This index is not shared, but the flag is part of the contract and the VMS
    is right to demand it.
  * Filter by the camera id, never by a camera NAME. The old service resolved
    names against its own table and silently ran the query UNFILTERED when it
    did not recognise one.
  * Timestamps: ISO-8601 for search hits (the VMS parses both forms), epoch
    MILLISECONDS for /detections, because the dashboard does `new Date(ts)`.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

import numpy as np

from .embedder import ClipEmbedder
from .plate_text import normalise as normalise_plate
from .store import Store

log = logging.getLogger("smartsearch.queries")

# Re-exported from the registry so there is one spelling of a domain name in
# this service. See index/domains.py.
from .domains import FACE, PERSON, VEHICLES, spec  # noqa: E402,F401

# Every read is filtered to rows that have not expired. Defence in depth, not
# the mechanism: index/retention.py sweeps expired rows and their crops, and
# this is what holds while a sweep is pending, disabled, behind, or failing.
# Without it a stalled sweep is invisible — the rows keep answering searches and
# the only symptom is disk. It is also what makes the sweep's own schedule a
# performance question rather than a correctness one.
#
# Cheap: `expires_at` is indexed (search_persons_expiry / search_vehicles_expiry)
# and, on the vector paths, this is one more predicate on a plan that already
# post-filters — migration 002 turned on HNSW iterative scan precisely so that
# filtered vector search does not come back empty.
_UNEXPIRED = "expires_at > now()"

#: How many plate reads the dashboard's feed carries, and how many ranked plates
#: sit beside it. Both are page-sized rather than configurable: this is a
#: dashboard, and an operator who wants the full history has the ANPR lookup.
_RECENT_PLATES = 24
_TOP_PLATES = 10

#: Rows fetched per requested result before repeats of one tracked object are
#: collapsed. Four covers a page where every object was seen four times; past
#: that the extra scan costs more than the fifth sighting is worth.
_CANDIDATE_FACTOR = 4
_MAX_CANDIDATES = 400

#: How far back a search looks, and how it grows when the first window cannot
#: fill the page. A VMS search is nearly always about what just happened, and
#: the scores make that impossible to get from ranking alone: a top-24 page for
#: "person" spans 0.2735 to 0.2620 with gaps as small as 0.00006, so yesterday
#: outranks a minute ago by noise. The window decides WHICH matches are on the
#: page; CLIP still decides which of them match at all.
#:
#: It only ever widens — a quiet camera at 3am still fills its page, it just
#: reaches further back to do it. The last step is None: everything the index
#: still holds, so nothing is unreachable.
_WINDOW_STEPS = (15 * 60, 30 * 60, 3600, 3 * 3600, 6 * 3600, 12 * 3600,
                 86_400, 3 * 86_400, 7 * 86_400, 30 * 86_400, None)


#: How far below a query's BEST match a result can score and still be a match.
#:
#: MEASURED on this index (2026-09-12; 53k person, 26k vehicle rows) by ranking
#: each query over everything retained and LOOKING at the crops:
#:
#:   "man in a red shirt"         #1 0.3486 ... #80 0.3081 — every rank checked
#:                                to #80 is a red shirt (0.041 below the best)
#:   "person carrying a backpack" #1 0.3457 ... #50 0.2941 — all carrying one
#:   "red motorcycle"             #1 0.3249 ... #80 0.2961 — all red motorcycles
#:   "person"                     #1 0.2735 ... #200 0.2555 — all people, as a
#:                                broad query should keep nearly everything
#:
#: So relevance survives about 0.05 below a query's own best and is long gone by
#: 0.10. The cards that prompted this — generic people at 0.20-0.23 shown for
#: "man in a red shirt" — sit 0.12-0.15 below that query's best of 0.3486.
#:
#: RELATIVE TO THE QUERY, NEVER ABSOLUTE, because CLIP cosine is not comparable
#: across queries: 0.2735 is the best score ANY crop gets for "person", and it
#: is below the 80th-best "man in a red shirt". One fixed floor therefore either
#: passes everything on one query or nothing on another — which is precisely
#: what an operator's Minimum Score cannot fix and why weak recent rows were
#: filling pages. That control stays exactly as it is: a floor the operator
#: sets, applied UNDER this bar, never replaced by it.
_RELEVANCE_MARGIN = 0.05
#: Rows read to learn what this query's best match scores, before any window is
#: imposed. Only the top one sets the bar; a handful is read because it costs
#: the same and makes the call legible in a log.
_REFERENCE_ROWS = 4


#: The vehicle classes the detector can actually emit — COCO 2/3/5/7, as
#: analytics filters them (detector.py `classes=[0, 2, 3, 5, 7]`). This is the
#: whole vocabulary: a word outside it describes a vehicle, it does not name a
#: class, and CLIP is what ranks descriptions.
_VEHICLE_CLASSES = ("car", "truck", "bus", "motorcycle")
#: Plurals, and nothing else. NOT synonyms: "van", "sedan", "scooter", "lorry"
#: and "bike" never appear in vehicle_type, so treating any of them as a class
#: would filter every row away and report it as "no matches" — the one failure
#: an operator cannot tell from an empty yard.
_CLASS_WORDS = {c: c for c in _VEHICLE_CLASSES}
_CLASS_WORDS.update({"cars": "car", "trucks": "truck", "buses": "bus",
                     "busses": "bus", "motorcycles": "motorcycle"})
_CLASS_RE = re.compile(r"\b(" + "|".join(sorted(_CLASS_WORDS, key=len, reverse=True))
                       + r")\b", re.IGNORECASE)

#: How far back "oldest first" may reach when no explicit range is given.
_OLDEST_FIRST_DAYS = 30

#: The two things a page can be ordered by. The operator picks ONE; the other
#: is never consulted, not even to break a tie.
BY_TIME, BY_SCORE = "time", "confidence"


def classes_named_in(query: str) -> list[str]:
    """The vehicle classes a query names outright, if any.

    WORD BOUNDARIES, NOT SUBSTRINGS. "person carrying a bag" contains "car"
    three letters in, and a substring match would quietly restrict that search
    to cars; the same boundary keeps "bus" out of "business".

    WHY THIS IS A HARD FILTER. CLIP ranks pixels and has no notion of the
    detector's class, so "white car" and "white truck" are nearly the same
    query to it — which is how a card labelled truck answered a search for a
    car. The class the operator typed is checked against the detector's own
    vehicle_type instead, and CLIP keeps the rest of the sentence: colour,
    context, everything that is not a class.

    A query naming no class ("white vehicle", "vehicle at the gate", "white")
    constrains nothing, which is what those queries mean.
    """
    return sorted({_CLASS_WORDS[m.group(1).lower()]
                   for m in _CLASS_RE.finditer(query or "")})


@dataclass(frozen=True)
class Order:
    """How a page is ordered once relevance has decided what is on it.

    ONE FIELD, NOT TWO. The operator says WHICH field arranges the page — when
    it happened, or how well it matched — and the other is not looked at. Two
    independent directions were tried both ways round and each arrangement made
    one of the controls inert: whichever field came second only ever broke ties,
    and neither timestamps nor float scores tie in practice. A mode plus a
    direction says exactly one thing, and the page does exactly that.

    This does not decide WHAT is on the page. Relevance, the operator's score
    floor and every filter have already run; this only arranges what survived,
    so no ordering can bring a weak match onto a page.
    """
    by: str = BY_TIME
    #: False = newest / strongest first, which is the default in both modes.
    ascending: bool = False

    @property
    def oldest_first(self) -> bool:
        """Time, ascending — the one combination that also bounds how far back
        the search reaches. Sorting by score never imposes that bound: it is a
        property of asking for the OLDEST thing, not of asking for the weakest.
        """
        return self.by == BY_TIME and self.ascending

    @classmethod
    def of(cls, sort_by: str = BY_TIME, sort_dir: str = "desc") -> "Order":
        by = BY_SCORE if str(sort_by).lower() in (BY_SCORE, "score", "similarity") \
            else BY_TIME
        return cls(by=by, ascending=str(sort_dir).lower() == "asc")


_ORDER_DEFAULT = Order()


def _candidate_limit(top_k: int) -> int:
    return max(1, min(top_k * _CANDIDATE_FACTOR, _MAX_CANDIDATES))


def _relevance_bar(reference: list[dict], score_threshold: float) -> float:
    """The lowest score that still counts as a match TO THIS QUERY.

    `reference` is the query ranked with no window over it, so its best row is
    the best this query can do at all. Everything within _RELEVANCE_MARGIN of
    that is a match and competes on recency; everything below it is a nearest
    neighbour the index had to return, not an answer.

    Falls back to the operator's own floor when there is nothing to compare
    against — an empty index has no scale, and inventing one would either hide
    every result or admit every one.
    """
    best = max((float(r["score"]) for r in reference), default=None)
    if best is None:
        return score_threshold
    return max(score_threshold, best - _RELEVANCE_MARGIN)


def _rank_key(order: Order = _ORDER_DEFAULT):
    """The ONE field the operator chose, then id. Nothing else.

    Relevance has already had its say by the time this runs: the query selected
    these rows by embedding distance, the relevance bar dropped the ones that do
    not match, and the threshold dropped the ones below the operator's floor.
    What is left is a set of MATCHES, and arranging them is a presentation
    choice the operator owns.

    THE INACTIVE FIELD IS NOT A SECOND KEY. A page sorted by time is ordered by
    time alone; one sorted by the match, by the match alone. Mixing them is what
    made each control look broken in turn — whichever came second could only
    break ties, and there are none to break. The id is the only tie-break, for
    rows that genuinely share an instant or a score, and it is what makes the
    same query over the same data produce the same page twice.
    """
    sign = 1 if order.ascending else -1
    if order.by == BY_SCORE:
        return lambda r: (sign * float(r["score"]), -int(r["id"]))
    return lambda r: (sign * r["ts"].timestamp(), -int(r["id"]))


def _recent_first(fetch, *, top_k: int, score_threshold: float,
                  windowed: bool, order: Order = _ORDER_DEFAULT) -> list[dict]:
    """Fill a page from the most recent window that can fill it.

    `fetch(since, until, limit)` runs the domain's vector query over one time
    slice, best-first. This walks _WINDOW_STEPS outward and stops as soon as it
    has enough distinct objects, so an ordinary daytime search touches the last
    quarter of an hour and nothing older.

    EACH PASS QUERIES ONLY THE SLICE IT ADDS — [since, until) — never the whole
    widened window again. Ten steps therefore cost one scan of the index's time
    range between them, not ten overlapping scans, and the slices being disjoint
    is what lets the rows be accumulated without deduplicating them.

    WHAT THIS IS NOT: a timestamp sort of the index. Every slice is ranked by
    embedding distance and cut to `limit`, so what lands on the page is the most
    query-like handful of a recent window — recency chooses the window and the
    order, relevance chooses the contents. `windowed=False` (an explicit from/to)
    skips all of it: the operator has named the window and it is not ours to
    widen.
    """
    limit = _candidate_limit(top_k)
    if not windowed:
        # The operator named the window, so the best match INSIDE it is the
        # scale: "the best answer this period holds, and everything close to
        # it". No second query — this fetch is already the whole range ranked.
        rows = fetch(None, None, limit)
        bar = _relevance_bar(rows, score_threshold)
        return _one_per_object([r for r in rows if float(r["score"]) >= bar],
                               top_k, order)

    # WHAT THIS QUERY CAN DO ANYWHERE, before any window narrows it. Without
    # this the bar would be set by whatever the last quarter of an hour happened
    # to contain, which is how a generic person three seconds old came to sit
    # above a red shirt from five minutes ago on a search for a red shirt.
    bar = _relevance_bar(fetch(None, None, _REFERENCE_ROWS), score_threshold)

    now = datetime.now(timezone.utc)
    kept: list[dict] = []
    objects: set[str] = set()
    until = None                       # the newest slice has no upper edge
    for step in _WINDOW_STEPS:
        since = now - timedelta(seconds=step) if step else None
        for r in fetch(since, until, limit):
            if float(r["score"]) < bar:
                # The slice came back best-first, so nothing after this one can
                # clear the bar either.
                break
            kept.append(r)
            objects.add(str(r.get("tracker_id") or f"row:{r['id']}"))
        # Enough DISTINCT OBJECTS, not rows: the page is objects, so filling it
        # with eight sightings of one person would stop the search too early.
        if len(objects) >= top_k:
            break
        until = since
    # However far it reached, the page is only ever the matches: a short page
    # means this query has few answers, and padding it with the nearest
    # non-matches would be the original complaint in a different order.
    return _one_per_object(kept, top_k, order)


def _one_per_object(rows: list[dict], top_k: int,
                    order: Order = _ORDER_DEFAULT) -> list[dict]:
    """One result per tracked object, in an order that does not move.

    WHY HERE AND NOT IN THE INDEXING POLICY. A long stay is several honest rows:
    analytics records an object again every 10 s and appearance dedup keeps only
    the looks that differ. All of them belong in the index — a forensic search
    should be able to find any of them. What an operator searching for someone
    wants BACK is the object, not every frame it appeared in, and that is a
    presentation decision. So it is made here, over rows the vector search has
    already ranked, and nothing about what is stored changes.

    THE KEY IS THE TRACKER ID, the only identity the index has, and it is not a
    permanent one: a track breaks on a long occlusion and the same person
    returns under a new id. So this UNDER-merges by design — two ids for one
    person show as two results, which is honest, whereas one id never covers two
    people. Rows written before tracking existed have no id and each stand for
    themselves.

    ORDERING is newest first — see _rank_key. The rows arrive best-first, which
    is what decides WHICH observation represents an object; the order they are
    then shown in is time.
    """
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(str(r.get("tracker_id") or f"row:{r['id']}"), []).append(r)

    picked: list[dict] = []
    for seen in groups.values():
        # The observation that matched best represents the object: its crop is
        # the one the operator asked to see. Ties break on id so the same page
        # comes back twice — the rows arrive from several time slices here, so
        # "first seen" is not a stable choice.
        rep = max(seen, key=lambda r: (float(r["score"]), -int(r["id"])))
        # How many observations of this object this result stands for. 1 is the
        # ordinary case; more is what the operator was spared.
        rep["sightings"] = len(seen)
        _agree_on_a_class(rep, seen)
        picked.append(rep)
    return sorted(picked, key=_rank_key(order))[:top_k]


def _agree_on_a_class(rep: dict, seen: list[dict]) -> None:
    """Let the object's observations vote on what the detector called it.

    THE DETECTOR AND CLIP ARE INDEPENDENT OPINIONS of one crop, and the weaker
    one is the class: measured on this index, 27% of tracks with more than one
    observation carry two different vehicle classes, and a third of "bus" boxes
    are taller than they are wide. So a card can say "truck" over a picture CLIP
    matched to "red motorcycle" — tracker d34ae9.0:4917 did exactly that, from a
    41x179 px sliver clipped by the frame edge at confidence 0.39.

    Collapsing several observations into one card is the chance to do better
    than whichever row happened to represent it: the class is the one its
    observations agree on, weighted by how sure the detector was each time.
    THE CONFIDENCE TRAVELS WITH THE LABEL — reporting the winning class beside
    some other observation's confidence would be a number that describes
    nothing. Everything else on the card still comes from `rep`.

    CLIP NEVER OVERRIDES THE CLASS: the vote is over detector opinions only.
    Person rows carry no class and are left alone.
    """
    if len(seen) < 2 or "vehicle_type" not in rep:
        return
    votes: dict[str, float] = {}
    for r in seen:
        label = r.get("vehicle_type")
        if label:
            votes[label] = votes.get(label, 0.0) + float(r.get("confidence") or 0.0)
    if not votes:
        return
    # sorted() first so an exact tie resolves the same way every time.
    winner = max(sorted(votes), key=lambda k: votes[k])
    rep["vehicle_type"] = winner
    rep["confidence"] = max(float(r.get("confidence") or 0.0)
                            for r in seen if r.get("vehicle_type") == winner)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_when(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class EncoderUnavailable(RuntimeError):
    """The text encoder could not be had, so this search has no honest answer.

    Raised rather than returning nothing: "no results" is the one reply a search
    must never give wrongly, and an operator who reads it as "this person was
    never here" cannot tell the difference.
    """


class QueryService:
    def __init__(self, store: Store, pool: ModelPool) -> None:
        self._store = store
        #: The encoder is not held here. It is loaded on first use and released
        #: once nothing has been indexed or searched for a while, so every query
        #: takes it from the pool rather than from a reference captured at boot
        #: — which would keep a hibernated model alive forever.
        self._pool = pool

    @property
    def available(self) -> bool:
        """Whether text search can be answered — loading the encoder if needed.

        This is "possible", not "loaded". Reporting a hibernating service as
        unavailable would make /health say search is broken on every quiet
        deployment, which is exactly the false alarm that trains operators to
        ignore it.
        """
        return self._pool.embedder_possible

    @property
    def plates_active(self) -> bool:
        """Whether this deployment reads plates. Surfaced so the UI can say
        "not enabled on this deployment" instead of showing an empty result,
        which reads as "this vehicle was never seen"."""
        return self._pool.plates_available

    def _embed_text(self, query: str):
        encoder = self._pool.acquire_embedder()
        if encoder is None:
            raise EncoderUnavailable(
                "the search encoder could not be loaded; see /health for why"
            )
        return encoder.embed_text(query)

    def _embed_image(self, image_bytes: bytes):
        """One crop → the SAME vector space text queries live in.

        CLIP's whole property is that its image and text encoders share an
        embedding space, so a search-by-example query is just a different
        front door into the identical index — same SQL, same scores, same
        threshold semantics. Raises ValueError on bytes that are not a
        decodable image: that is the caller's 400, not an encoder outage.
        """
        import io

        from PIL import Image, UnidentifiedImageError

        encoder = self._pool.acquire_embedder()
        if encoder is None:
            raise EncoderUnavailable(
                "the search encoder could not be loaded; see /health for why"
            )
        try:
            img = Image.open(io.BytesIO(image_bytes))
            img.load()
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError(f"not a decodable image: {exc}") from exc
        return encoder.embed_images([img.convert("RGB")])[0]

    # ── search ───────────────────────────────────────────────────────────────
    def search_people(self, *, query: str, top_k: int, score_threshold: float,
                      sensor_id: Optional[str] = None,
                      sensor_ids: Optional[Sequence[str]] = None,
                      time_from: Optional[str] = None,
                      time_to: Optional[str] = None,
                      sort_by: str = BY_TIME,
                      sort_dir: str = "desc") -> list[dict]:
        return self._people_by_vector(
            self._embed_text(query), top_k=top_k, score_threshold=score_threshold,
            sensor_id=sensor_id, sensor_ids=sensor_ids,
            time_from=time_from, time_to=time_to,
            sort_by=sort_by, sort_dir=sort_dir,
        )

    def search_people_by_image(self, *, image: bytes, top_k: int,
                               score_threshold: float,
                               sensor_id: Optional[str] = None,
                               sensor_ids: Optional[Sequence[str]] = None,
                               time_from: Optional[str] = None,
                               time_to: Optional[str] = None,
                               sort_by: str = BY_TIME,
                               sort_dir: str = "desc") -> list[dict]:
        """Search-by-example: a crop (typically a playback annotation) instead
        of a sentence. Everything after the embedding is shared with the text
        path — deliberately, so the two can never rank or filter differently."""
        return self._people_by_vector(
            self._embed_image(image), top_k=top_k, score_threshold=score_threshold,
            sensor_id=sensor_id, sensor_ids=sensor_ids,
            time_from=time_from, time_to=time_to,
            sort_by=sort_by, sort_dir=sort_dir,
        )

    def _people_by_vector(self, embedding, *, top_k: int, score_threshold: float,
                          sensor_id: Optional[str] = None,
                          sensor_ids: Optional[Sequence[str]] = None,
                          time_from: Optional[str] = None,
                          time_to: Optional[str] = None,
                          sort_by: str = BY_TIME,
                          sort_dir: str = "desc") -> list[dict]:
        order = Order.of(sort_by, sort_dir)
        vec = str(embedding.tolist())
        where, params = [_UNEXPIRED], {}
        if sensor_id:
            where.append("sensor_id = %(sensor_id)s")
            params["sensor_id"] = sensor_id
        elif sensor_ids is not None:
            # An EMPTY list means "this VMS has no searchable cameras", which is
            # a real answer: nothing may come back. It must not be read as "no
            # filter" — that would return the whole index.
            where.append("sensor_id = ANY(%(sensor_ids)s)")
            params["sensor_ids"] = list(sensor_ids)
        # Named once, because the recent window turns on whether the operator
        # gave a range at all — see _recent_first.
        frm, to = _parse_when(time_from), _parse_when(time_to)
        if frm is not None:
            where.append("ts >= %(frm)s")
            params["frm"] = frm
        if to is not None:
            where.append("ts <= %(to)s")
            params["to"] = to

        # OLDEST FIRST IS BOUNDED TO 30 DAYS. Newest-first walks outward from
        # now and stops the moment the page is full, so it never reads more of
        # the index than it needs. Oldest-first has no such stopping point:
        # unbounded it would mean "the oldest match in everything retained",
        # which is a scan of the whole index and almost never what was asked.
        # An explicit from/to overrides it, as it overrides the recent window.
        if order.oldest_first and frm is None:
            frm = datetime.now(timezone.utc) - timedelta(days=_OLDEST_FIRST_DAYS)
            where.append("ts >= %(frm)s")
            params["frm"] = frm

        def fetch(since, until, limit):
            """One time slice, best-first. Over-fetched, because the page is
            top_k OBJECTS and several rows can belong to one of them."""
            w, p = list(where), dict(params, vec=vec, limit=limit)
            if since is not None:
                w.append("ts >= %(w_since)s")
                p["w_since"] = since
            if until is not None:
                w.append("ts < %(w_until)s")
                p["w_until"] = until
            return self._store.fetch(f"""
                SELECT id, sensor_id, ts, tracker_id, confidence, frame_number,
                       pad_index, bbox, frame_path IS NOT NULL AS has_frame,
                       1 - (embedding <=> %(vec)s) AS score
                FROM search_persons
                WHERE {' AND '.join(w)}
                ORDER BY embedding <=> %(vec)s, ts DESC, id DESC
                LIMIT %(limit)s
            """, p)

        # An explicit from/to IS the window; otherwise start recent and widen.
        rows = _recent_first(fetch, top_k=top_k, score_threshold=score_threshold,
                             windowed=frm is None and to is None, order=order)
        return [
            {
                "id": str(r["id"]),
                "score": float(r["score"]),
                "sensor_id": r["sensor_id"],
                # The VMS tries sensor_id then camera_name to place a hit. Both
                # are the slug here; there is no separate camera table to drift.
                "camera_name": r["sensor_id"],
                "timestamp": _iso(r["ts"]),
                "tracker_id": r["tracker_id"],
                "confidence": r["confidence"],
                "frame_number": r["frame_number"],
                "pad_index": r["pad_index"],
                # Normalised 0-1 of the SOURCE FRAME, so the card can draw it
                # over the stored frame without knowing either resolution.
                "bbox": list(r["bbox"]) if r["bbox"] else None,
                # Whether the whole frame was kept for this row (migration 005).
                # False for anything older, whose card shows the crop instead.
                "has_frame": bool(r["has_frame"]),
                # Observations of this object this result stands for.
                "sightings": r.get("sightings", 1),
            }
            for r in rows
        ]

    # ── faces ───────────────────────────────────────────────────────────────
    def search_faces(self, *, vector, top_k: int, score_threshold: float,
                     camera_ids: Optional[Sequence[str]] = None,
                     time_from: Optional[str] = None,
                     time_to: Optional[str] = None,
                     min_width_px: int = 0,
                     model: Optional[str] = None) -> list[dict]:
        """Nearest stored faces to an already-embedded query face.

        NO TEXT PATH, and that is not an omission. These vectors are SFace, not
        CLIP: there is no text encoder for this space, so "a man in a blue
        shirt" cannot be turned into a face vector, and a NAME certainly cannot
        — the thing operators will try first. The person domain already had to
        grow a name-shaped-query warning for exactly that reason; here the
        endpoint simply does not accept text at all, which is the honest shape.

        `min_width_px` filters on the stored face size rather than on score,
        because the two answer different questions: score says the match is
        close, width says the row was worth storing. Measured rank-1 retrieval
        was 24% under 40px against 40% at 64-111px, so an operator who wants
        only faces worth looking at is asking about width.
        """
        d = spec(FACE)
        where = [_UNEXPIRED]
        params: dict = {}
        if camera_ids is not None:
            # Same rule as every other domain: an EMPTY list is a real scope
            # that can match nothing, never "no filter". See the note in
            # search_people — an index that read it the other way once answered
            # with somebody else's collection.
            where.append(f"{d.camera_column} = ANY(%(camera_ids)s)")
            params["camera_ids"] = list(camera_ids)
        if (frm := _parse_when(time_from)) is not None:
            where.append("ts >= %(frm)s")
            params["frm"] = frm
        if (to := _parse_when(time_to)) is not None:
            where.append("ts <= %(to)s")
            params["to"] = to
        if min_width_px > 0:
            where.append("face_width_px >= %(minw)s")
            params["minw"] = int(min_width_px)
        if model:
            # THE ACTIVE MODEL ONLY. Vectors from a different embedder share no
            # space with this query, so a row from one would rank by noise —
            # and rank plausibly, which is worse than not appearing. A swapped
            # model therefore returns fewer results until the re-index catches
            # up, which is the failure an operator can see and reason about.
            where.append("embedding_model = %(model)s")
            params["model"] = model

        sql = f"""
            SELECT id, {d.camera_column} AS camera_id, ts, tracker_id, confidence,
                   face_width_px, bbox, 1 - (embedding <=> %(vec)s) AS score
            FROM {d.table}
            WHERE {' AND '.join(where)}
            ORDER BY embedding <=> %(vec)s
            LIMIT %(limit)s
        """
        # `.tolist()`, NOT `list(...)`. Both look like a list of floats and only
        # one of them is: under NumPy 2 the elements of a plain list() keep
        # their numpy type, so str() renders "[np.float64(0.0088), ...]" and
        # pgvector rejects the literal. tolist() converts to Python floats,
        # which is why every other query in this file already uses it.
        params.update(
            vec=str(np.asarray(vector, dtype="float32").ravel().tolist()),
            limit=max(1, top_k))
        rows = self._store.fetch(sql, params)
        return [
            {
                "id": str(r["id"]),
                "score": float(r["score"]),
                "camera_id": r["camera_id"],
                "camera_name": r["camera_id"],
                "timestamp": _iso(r["ts"]),
                "tracker_id": r["tracker_id"],
                "confidence": r["confidence"],
                "face_width_px": r["face_width_px"],
                "bbox": list(r["bbox"]) if r["bbox"] else None,
            }
            for r in rows if float(r["score"]) >= score_threshold
        ]

    def recent_faces(self, *, limit: int, camera_ids: Optional[Sequence[str]] = None,
                     min_width_px: int = 0, model: Optional[str] = None) -> list[dict]:
        """The gallery behind the Faces tab before anyone has searched.

        A domain whose only query is "upload a photo" is unusable until the
        operator already has a photo, so the tab opens on what it has actually
        collected. This is also the honest way to show a camera yielding
        nothing: an empty gallery says so, a search box does not.
        """
        d = spec(FACE)
        where = [_UNEXPIRED]
        params: dict = {"limit": max(1, min(int(limit), 200))}
        if camera_ids is not None:
            where.append(f"{d.camera_column} = ANY(%(camera_ids)s)")
            params["camera_ids"] = list(camera_ids)
        if min_width_px > 0:
            where.append("face_width_px >= %(minw)s")
            params["minw"] = int(min_width_px)
        if model:
            # The gallery is scoped too, so what it shows is what a search can
            # actually return. An unscoped gallery after a model swap would
            # display rows that no query can reach.
            where.append("embedding_model = %(model)s")
            params["model"] = model
        rows = self._store.fetch(f"""
            SELECT id, {d.camera_column} AS camera_id, ts, tracker_id, confidence,
                   face_width_px, bbox
            FROM {d.table}
            WHERE {' AND '.join(where)}
            ORDER BY ts DESC
            LIMIT %(limit)s
        """, params)
        return [
            {
                "id": str(r["id"]),
                "camera_id": r["camera_id"],
                "camera_name": r["camera_id"],
                "timestamp": _iso(r["ts"]),
                "tracker_id": r["tracker_id"],
                "confidence": r["confidence"],
                "face_width_px": r["face_width_px"],
                "bbox": list(r["bbox"]) if r["bbox"] else None,
                "score": None,
            }
            for r in rows
        ]

    def search_vehicles(self, *, query: str, top_k: int, score_threshold: float,
                        plate: Optional[str] = None,
                        vehicle_type: Optional[str] = None,
                        color: Optional[str] = None,
                        camera_ids: Optional[Sequence[str]] = None,
                        time_from: Optional[str] = None,
                        time_to: Optional[str] = None,
                        sort_by: str = BY_TIME,
                        sort_dir: str = "desc") -> list[dict]:
        """Vehicles by description. TIME BOUNDS ARRIVED WITH THE RECENT WINDOW:
        this path had none, so the from/to the UI has always offered reached the
        person search and silently did nothing here."""
        order = Order.of(sort_by, sort_dir)
        vec = str(self._embed_text(query).tolist())
        where, params = [_UNEXPIRED], {}
        if plate:
            # Trigram, not vector similarity. A plate is a string, and ILIKE
            # tolerates the OCR near-misses ("0" for "O") that an embedding
            # cannot express at all.
            where.append("plate ILIKE %(plate)s")
            params["plate"] = f"%{plate}%"
        named = classes_named_in(query)
        if vehicle_type:
            # The dropdown is the operator saying it outright, so it wins over
            # anything read out of their sentence.
            where.append("vehicle_type = %(vtype)s")
            params["vtype"] = vehicle_type
        elif named:
            # "white car" means a car. CLIP cannot be told that — see
            # classes_named_in — so the detector's own class is checked here and
            # CLIP is left to rank what remains: colour, context, everything
            # that is not a class. Several named classes widen rather than
            # contradict ("car or truck" asks for both).
            where.append("vehicle_type = ANY(%(named)s)")
            params["named"] = named
            log.info("search.vehicles.class_filter query=%r classes=%s", query, named)
        if color:
            where.append("color = %(color)s")
            params["color"] = color
        if camera_ids is not None:
            where.append("camera_id = ANY(%(camera_ids)s)")
            params["camera_ids"] = list(camera_ids)

        frm, to = _parse_when(time_from), _parse_when(time_to)
        if frm is not None:
            where.append("ts >= %(frm)s")
            params["frm"] = frm
        if to is not None:
            where.append("ts <= %(to)s")
            params["to"] = to

        # OLDEST FIRST IS BOUNDED TO 30 DAYS. Newest-first walks outward from
        # now and stops the moment the page is full, so it never reads more of
        # the index than it needs. Oldest-first has no such stopping point:
        # unbounded it would mean "the oldest match in everything retained",
        # which is a scan of the whole index and almost never what was asked.
        # An explicit from/to overrides it, as it overrides the recent window.
        if order.oldest_first and frm is None:
            frm = datetime.now(timezone.utc) - timedelta(days=_OLDEST_FIRST_DAYS)
            where.append("ts >= %(frm)s")
            params["frm"] = frm

        def fetch(since, until, limit):
            w, p = list(where), dict(params, vec=vec, limit=limit)
            if since is not None:
                w.append("ts >= %(w_since)s")
                p["w_since"] = since
            if until is not None:
                w.append("ts < %(w_until)s")
                p["w_until"] = until
            return self._store.fetch(f"""
                SELECT id, camera_id, ts, plate, vehicle_type, color, confidence,
                       tracker_id, bbox, frame_path IS NOT NULL AS has_frame,
                       1 - (embedding <=> %(vec)s) AS score
                FROM search_vehicles
                WHERE {' AND '.join(w)}
                ORDER BY embedding <=> %(vec)s, ts DESC, id DESC
                LIMIT %(limit)s
            """, p)

        rows = _recent_first(fetch, top_k=top_k, score_threshold=score_threshold,
                             windowed=frm is None and to is None, order=order)
        return [
            {
                "id": str(r["id"]),
                "score": float(r["score"]),
                "camera_id": r["camera_id"],
                "timestamp": _iso(r["ts"]),
                "plate": r["plate"],
                "vehicle_type": r["vehicle_type"],
                "color": r["color"],
                "confidence": r["confidence"],
                # Which physical object this is, as the person side has always
                # reported. Stored since migration 004; never surfaced here.
                "tracker_id": r["tracker_id"],
                "bbox": list(r["bbox"]) if r["bbox"] else None,
                "has_frame": bool(r["has_frame"]),
                "sightings": r.get("sightings", 1),
            }
            for r in rows
        ]

    # ── plate lookup ─────────────────────────────────────────────────────────
    def search_plates(self, *, plate: str, camera_ids: Optional[Sequence[str]],
                      time_from: Optional[str] = None,
                      time_to: Optional[str] = None,
                      limit: int = 200) -> dict:
        """Sightings of a plate, grouped by plate, newest first.

        NO EMBEDDER. This is a substring match on an indexed text column, and
        keeping it that way means plate lookup — the most precise identifier the
        product has — still works on a box where the encoder failed to load or
        is too big to run. Ranking by visual similarity would be noise here:
        when you know the plate, "looks like a white van" is not a tie-break.
        """
        needle = normalise_plate(plate)
        if not needle:
            return {"plates": [], "plates_active": self.plates_active,
                    "scoped": True, "reason": "empty plate query"}

        where = [_UNEXPIRED, "plate IS NOT NULL", "plate ILIKE %(needle)s"]
        params: dict[str, Any] = {"needle": f"%{needle}%"}
        if camera_ids is not None:
            where.append("camera_id = ANY(%(ids)s)")
            params["ids"] = list(camera_ids)
        if (frm := _parse_when(time_from)) is not None:
            where.append("ts >= %(frm)s")
            params["frm"] = frm
        if (to := _parse_when(time_to)) is not None:
            where.append("ts <= %(to)s")
            params["to"] = to

        rows = self._store.fetch(
            f"""SELECT id, plate, camera_id, ts, vehicle_type, color, confidence, bbox
                FROM search_vehicles
                WHERE {' AND '.join(where)}
                ORDER BY plate, ts DESC
                LIMIT %(limit)s""",
            {**params, "limit": max(1, min(limit, 2000))},
        )

        grouped: dict[str, list[dict]] = {}
        for r in rows:
            grouped.setdefault(r["plate"], []).append({
                "id": str(r["id"]),
                "camera_id": r["camera_id"],
                "timestamp": _iso(r["ts"]),
                "vehicle_type": r["vehicle_type"],
                "color": r["color"],
                "confidence": r["confidence"],
                "bbox": list(r["bbox"]) if r["bbox"] else None,
            })
        plates = [
            {
                "plate": p,
                "sightings": s,
                "count": len(s),
                # s is already newest-first within a plate.
                "last_seen": s[0]["timestamp"],
                "first_seen": s[-1]["timestamp"],
                "cameras": sorted({x["camera_id"] for x in s}),
            }
            for p, s in grouped.items()
        ]
        plates.sort(key=lambda p: p["last_seen"], reverse=True)
        return {"plates": plates, "plates_active": self.plates_active, "scoped": True}

    # ── stats ────────────────────────────────────────────────────────────────
    def stats(self, domain: str, camera_ids: Optional[Sequence[str]]) -> dict:
        d = spec(domain)
        table, col = d.table, d.camera_column
        if camera_ids is None:
            sql = f"SELECT count(*) AS n FROM {table} WHERE {_UNEXPIRED}"
            params = {}
        else:
            sql = (f"SELECT count(*) AS n FROM {table} "
                   f"WHERE {_UNEXPIRED} AND {col} = ANY(%(ids)s)")
            params = {"ids": list(camera_ids)}
        rows = self._store.fetch(sql, params)
        return {
            "plates_active": self.plates_active if domain == VEHICLES else None,
            "vectors_count": int(rows[0]["n"]) if rows else 0,
            # Always true: the filter given is always the filter applied. The
            # flag exists because an index that ignored it used to answer with
            # somebody else's collection.
            "scoped": True,
            "status": "green",
        }

    def face_models(self) -> dict[str, int]:
        """Rows per embedding model. How far a re-index has got, from the data
        rather than from someone's memory of when the swap happened."""
        d = spec(FACE)
        rows = self._store.fetch(
            f"SELECT coalesce(embedding_model, 'unknown') AS m, count(*) AS n "
            f"FROM {d.table} WHERE {_UNEXPIRED} GROUP BY 1")
        return {r["m"]: int(r["n"]) for r in rows}

    # ── crop image ───────────────────────────────────────────────────────────
    def crop_path(self, domain: str, hit_id: str) -> Optional[str]:
        table = spec(domain).table
        try:
            ident = int(hit_id)
        except (TypeError, ValueError):
            return None
        rows = self._store.fetch(
            f"SELECT crop_path FROM {table} WHERE id = %(id)s AND {_UNEXPIRED}",
            {"id": ident},
        )
        return rows[0]["crop_path"] if rows else None

    def frame_path(self, domain: str, hit_id: str) -> Optional[str]:
        """The whole frame a detection came from — see migrations/005. None for
        a row written before frames were kept."""
        table = "search_persons" if domain == PERSON else "search_vehicles"
        try:
            ident = int(hit_id)
        except (TypeError, ValueError):
            return None
        rows = self._store.fetch(
            f"SELECT frame_path FROM {table} WHERE id = %(id)s AND {_UNEXPIRED}",
            {"id": ident},
        )
        return rows[0]["frame_path"] if rows else None

    # ── detections feed + summary ────────────────────────────────────────────
    def detections(self, *, since_ms: int, limit: int, tz_offset_min: int,
                   camera_ids: Optional[Sequence[str]],
                   domains: Optional[Sequence[str]] = None) -> dict:
        """Aggregated INSIDE the index, because the VMS cannot filter afterwards.

        Hour buckets are in the viewer's local time — the dashboard sends its own
        offset, so a night shift does not straddle two bars.

        `domains` narrows THE FEED ONLY, never the summary. On a typical site
        people outnumber vehicles several times over, so the newest N detections
        are almost all people and the vehicle half of the product is invisible —
        measured here, 17 of 18 cards. Both domains are already fetched and the
        merge then discards the vehicles, so this filter costs no extra query.

        The summary deliberately stays whole. A filter that also moved the
        totals, the hourly chart and the top cameras would let an operator read
        "1,906 vehicles" as the site's whole activity.
        """
        since = datetime.fromtimestamp(max(0, since_ms) / 1000, tz=timezone.utc)
        ids = list(camera_ids) if camera_ids is not None else None
        shift = timedelta(minutes=tz_offset_min)

        summary = {"total": 0, "by_domain": {}, "by_type": {},
                   "top_cameras": [], "by_hour": [],
                   # Plate reads are a THIRD thing, not a vehicle sub-count: a
                   # vehicle is indexed whether or not its plate could be read,
                   # so reporting them inside by_type would double-count.
                   "plates_read": 0, "distinct_plates": 0,
                   # ...and vehicles_seen is what makes the count readable. A
                   # bare "41 plates" cannot be told apart from a broken
                   # localiser; "41 of 380 vehicles" can.
                   "vehicles_seen": 0}
        recent: list[dict] = []
        cam_counts: dict[str, int] = {}
        hour_counts: dict[str, int] = {}

        # plate_expr differs per domain because only vehicles carry one; a
        # literal NULL keeps the recent-feed query a single shape.
        for domain, table, col, type_expr, plate_expr in (
            (PERSON, "search_persons", "sensor_id", "'person'", "NULL::text"),
            (VEHICLES, "search_vehicles", "camera_id",
             "coalesce(vehicle_type, 'vehicle')", "plate"),
        ):
            where = [_UNEXPIRED, "ts >= %(since)s"]
            params: dict[str, Any] = {"since": since}
            if ids is not None:
                where.append(f"{col} = ANY(%(ids)s)")
                params["ids"] = ids
            clause = " AND ".join(where)

            agg = self._store.fetch(
                f"""SELECT {col} AS camera, {type_expr} AS type, count(*) AS n,
                           to_char(ts + %(shift)s, 'HH24:00') AS hour
                    FROM {table} WHERE {clause}
                    GROUP BY 1, 2, 4""",
                {**params, "shift": shift},
            )
            for r in agg:
                n = int(r["n"])
                summary["total"] += n
                summary["by_domain"][domain] = summary["by_domain"].get(domain, 0) + n
                # The retiring index spelled this key "vehicle" while spelling
                # the route "/vehicles", and the dashboard reads the singular.
                # Emit both rather than silently zeroing a panel on cutover.
                if domain == VEHICLES:
                    summary["by_domain"]["vehicle"] = summary["by_domain"][domain]
                    summary["vehicles_seen"] += n
                summary["by_type"][r["type"]] = summary["by_type"].get(r["type"], 0) + n
                cam_counts[r["camera"]] = cam_counts.get(r["camera"], 0) + n
                hour_counts[r["hour"]] = hour_counts.get(r["hour"], 0) + n

            recent.extend(
                {
                    "id": str(r["id"]),
                    "domain": domain,
                    "camera_id": r["camera"],
                    "type": r["type"],
                    # Epoch MILLISECONDS: the dashboard calls new Date(ts).
                    "timestamp": int(r["ts"].timestamp() * 1000),
                    # On the crop card itself, so a plate is attached to the
                    # vehicle it was read from rather than only to a tally.
                    "plate": r["plate"] or None,
                    # Which physical object this observation belongs to. NULL
                    # for every row written before index/tracking.py existed,
                    # which is the honest answer for them — see migrations/004.
                    "tracker_id": r["tracker_id"],
                    # Where the object sat, normalised 0-1, and whether its
                    # whole frame was kept: the feed draws this box over that
                    # frame rather than showing the crop.
                    "bbox": [float(v) for v in r["bbox"]] if r["bbox"] else None,
                    "has_frame": bool(r["has_frame"]),
                    # Every object that frame held, so one card can box all of
                    # them. None for rows written before migration 006, which
                    # the page falls back to drawing this row's box alone.
                    "frame_boxes": r["frame_boxes"] or None,
                }
                for r in self._store.fetch(
                    f"""SELECT id, {col} AS camera, {type_expr} AS type, ts,
                               {plate_expr} AS plate, tracker_id, bbox,
                               frame_path IS NOT NULL AS has_frame, frame_boxes
                        FROM {table} WHERE {clause}
                        ORDER BY ts DESC LIMIT %(limit)s""",
                    {**params, "limit": max(1, limit)},
                )
            )

        # Plates over the same window and camera scope.
        pwhere = [_UNEXPIRED, "ts >= %(since)s", "plate IS NOT NULL"]
        pparams: dict[str, Any] = {"since": since}
        if ids is not None:
            pwhere.append("camera_id = ANY(%(ids)s)")
            pparams["ids"] = ids
        prow = self._store.fetch(
            f"""SELECT count(*) AS n, count(DISTINCT plate) AS d
                FROM search_vehicles WHERE {' AND '.join(pwhere)}""",
            pparams,
        )
        if prow:
            summary["plates_read"] = int(prow[0]["n"])
            summary["distinct_plates"] = int(prow[0]["d"])

        # THE PLATES THEMSELVES, not only how many there were. A count answers
        # "is ANPR working"; the numbers answer "who came through", which is the
        # question the feature exists for — and until now the only way to see
        # one was to already know it and look it up.
        pclause = " AND ".join(pwhere)
        recent_plates = [
            {
                "id": str(r["id"]),
                "plate": r["plate"],
                "confidence": (round(float(r["plate_confidence"]), 3)
                               if r["plate_confidence"] is not None else None),
                "camera_id": r["camera"],
                "vehicle_type": r["vehicle_type"] or "vehicle",
                "timestamp": int(r["ts"].timestamp() * 1000),
            }
            for r in self._store.fetch(
                f"""SELECT id, plate, plate_confidence, camera_id AS camera,
                           vehicle_type, ts
                    FROM search_vehicles WHERE {pclause}
                    ORDER BY ts DESC LIMIT %(plimit)s""",
                {**pparams, "plimit": _RECENT_PLATES},
            )
        ]
        # Ranked by sightings, not recency: one plate seen nine times is the
        # thing worth surfacing on a gate camera, and it is invisible in a feed
        # ordered by time. Ties break toward the most recent so a page of
        # single-sighting plates is still in a useful order.
        top_plates = [
            {
                "plate": r["plate"],
                "count": int(r["n"]),
                "cameras": int(r["cams"]),
                "first_seen": int(r["first_ts"].timestamp() * 1000),
                "last_seen": int(r["last_ts"].timestamp() * 1000),
            }
            for r in self._store.fetch(
                f"""SELECT plate, count(*) AS n, count(DISTINCT camera_id) AS cams,
                           min(ts) AS first_ts, max(ts) AS last_ts
                    FROM search_vehicles WHERE {pclause}
                    GROUP BY plate
                    ORDER BY n DESC, max(ts) DESC LIMIT %(tlimit)s""",
                {**pparams, "tlimit": _TOP_PLATES},
            )
        ]

        recent.sort(key=lambda d: d["timestamp"], reverse=True)
        if domains:
            wanted = set(domains)
            recent = [r for r in recent if r["domain"] in wanted]
        summary["top_cameras"] = [
            {"camera": c, "count": n}
            for c, n in sorted(cam_counts.items(), key=lambda kv: -kv[1])[:10]
        ]
        # Every hour present, in order, so the chart has an axis rather than
        # only the hours that happened to have activity.
        summary["by_hour"] = [
            {"hour": f"{h:02d}:00", "count": hour_counts.get(f"{h:02d}:00", 0)}
            for h in range(24)
        ]
        return {
            "scoped": True,
            "summary": summary,
            "recent": recent[:limit],
            # The plate reads themselves. Separate from `recent`, which is the
            # detection feed: a vehicle appears there whether or not its plate
            # could be read, and folding the two would make "no plate" and "not
            # a vehicle" look like the same row.
            "recent_plates": recent_plates,
            "top_plates": top_plates,
            # False means no plate could have been read, so a zero is not a
            # statement about traffic. The dashboard renders that differently.
            "plates_active": self.plates_active,
        }
