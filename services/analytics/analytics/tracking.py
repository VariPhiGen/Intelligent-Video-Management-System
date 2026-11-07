"""tracking.py — is this the same physical object as before?

That question, and only that question. Whether an observation of an already
identified object earns a row is index/indexing_policy.py's business. The old
temporal gate conflated the two and that is why it behaved oddly: its 10 s
window was simultaneously the identity timeout AND the re-index interval, so a
person who never moved was re-indexed every 10 s for no reason anyone could
name.

WHY NOT IoU, AND THEREFORE WHY NOT ByteTrack / SORT / BoT-SORT. Measured
2026-09-07 by replaying 16 NVR segments at both 1 and 2 FPS: an IoU matcher
fails to associate 100% of walking-person pairs at 1 FPS and 79.2% at 2 FPS,
and 62.5% of walking pairs at 2 FPS have literally ZERO overlap — no threshold
reaches those. This is not a tuning problem, it is the wrong metric for a
sampled pipeline. It also explains the 2026-08-31 finding recorded in dedup.py,
where ByteTrack at 1 FPS found 5 identities against a 15 FPS reference's 8: it
MERGED people, which for forensic search is the worse failure.

WHAT WE USE INSTEAD is displacement normalised by object size, plus size change
— the same family as the temporal gate's validated centre-distance-in-heights,
with Frigate's two refinements: measure from the BOTTOM CENTRE (feet and wheels
sit on the ground plane; a centroid drifts when a bounding box grows upward)
and treat a change of scale as distance in its own right (a passing car and a
parked car can share a centre, but not a size trajectory).

WHY NOT NORFAIR, whose ideas these are. Its Kalman runs at dt = 1 FRAME, and
Frigate's constants are fitted at roughly 5 FPS, so none of its tuning
transfers to 2 FPS — the library would have saved us the filter, not the work.
Against that it costs a scipy dependency on an image already at 2.4 GB, ships
ReID and camera-motion machinery we would not use, and we already owned and had
measured the hard part: greedy one-to-one assignment on a size-normalised
distance, validated on 5,079 real detections. If the sweep ever shows this
tracker inadequate, Norfair with a Frigate-shaped distance function is the
documented fallback and nothing above it would have to change.

EVERY PARAMETER HERE IS IN SECONDS OR OBJECT HEIGHTS. Never frames, never
pixels. That is what stops a change of max_sample_fps from silently re-tuning
the tracker, and it is the single most important property of this module.
"""
from __future__ import annotations

import math
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Sequence

#: Lifecycle states. CONFIRMED is the moment a track becomes real; the next
#: update moves it to ACTIVE or STATIONARY. LOST means "no detection this
#: frame but not yet old enough to give up on"; expiry removes the track
#: entirely rather than leaving a state for it.
TENTATIVE = "TENTATIVE"
CONFIRMED = "CONFIRMED"
ACTIVE = "ACTIVE"
STATIONARY = "STATIONARY"
LOST = "LOST"

#: How far behind the freshest camera a camera's clock may fall before its
#: queued retirements are announced anyway. The broker keeps a camera within
#: ~5 s of real time (its skew limit), so 30 s behind means it stopped sending.
STALE_CAMERA_SECONDS = 30.0


@dataclass
class Track:
    """One physical object, as far as geometry can tell.

    Position is the BOTTOM CENTRE of the box and size is (w, h), all in
    whatever units the detector reports — pixels here. Velocity is in those
    units per SECOND, never per frame.
    """
    track_id: str
    domain: str
    state: str
    first_seen: float
    last_seen: float
    cx: float
    by: float
    w: float
    h: float
    vx: float = 0.0
    vy: float = 0.0
    hits: int = 1
    #: Where the track was when it last counted as having moved, and when that
    #: was. Stationary classification is measured against these rather than
    #: against the previous frame, so a slow drift accumulates instead of
    #: reading as stillness forever.
    still_cx: float = 0.0
    still_by: float = 0.0
    still_since: float = 0.0
    #: Best crop geometry seen so far, for ANPR/face frame selection.
    best_score: float = -1.0

    @property
    def confirmed(self) -> bool:
        return self.state in (CONFIRMED, ACTIVE, STATIONARY, LOST)

    def predict(self, ts: float) -> tuple[float, float]:
        """Where the object should be at `ts`, given constant velocity.

        Deliberately not a Kalman filter. At 2 FPS the useful content of
        prediction is "roughly where is it heading", and the measured
        association margin (p99 cost 1.63 against a 1.75 threshold) does not
        justify carrying a covariance model. If the sweep ever shows failures
        that better prediction would have caught, that is the moment to
        revisit — not before.
        """
        dt = max(0.0, ts - self.last_seen)
        return self.cx + self.vx * dt, self.by + self.vy * dt

    def snapshot(self) -> dict:
        return {"track_id": self.track_id, "domain": self.domain,
                "state": self.state, "hits": self.hits,
                "age_seconds": round(self.last_seen - self.first_seen, 2)}


@dataclass
class TrackedDetection:
    """A detection and the track it belongs to. What the pipeline consumes."""
    detection: object
    track: Track
    #: True on the frame a track was created — the first sight of an object.
    is_new: bool


def _geometry(xyxy) -> tuple[float, float, float, float]:
    """(bottom-centre x, bottom y, width, height) from a detector box."""
    x1, y1, x2, y2 = xyxy
    w = max(1.0, float(x2 - x1))
    h = max(1.0, float(y2 - y1))
    return (float(x1 + x2) / 2.0, float(y2), w, h)


@dataclass
class _Lost:
    """A track that died recently, kept in case the same object comes back.

    THE TRACKER DELIBERATELY HAS NO APPEARANCE MODEL, so this is geometry and a
    clock: an object that reappears where another just vanished, within a couple
    of seconds, is the same object far more often than not. Measured on four
    minutes of gate footage: 89 ids became 83 at 5 FPS and 86 became 59 at
    2.5 FPS (with the looser association cost), and no crossing was invented.
    """
    track_id: str
    first_seen: float
    last_seen: float
    #: When the identity was dropped. The window runs from HERE, not from the
    #: last sighting: a track only dies `max_age` after it was last seen, so a
    #: window measured from the sighting would already have expired.
    retired_at: float
    box: tuple[float, float, float, float]
    confirmed: bool
    hits: int


def _box_of(cx: float, by: float, w: float, h: float) -> tuple[float, float, float, float]:
    return (cx - w / 2.0, by - h, cx + w / 2.0, by)


def _iou(a, b) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    union = ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)
    return inter / union if union > 0 else 0.0


def _centre_gap_in_heights(a, b) -> float:
    ax, ay = (a[0] + a[2]) / 2.0, (a[1] + a[3]) / 2.0
    bx, by_ = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
    scale = max(1e-6, ((a[3] - a[1]) + (b[3] - b[1])) / 2.0)
    return math.hypot(ax - bx, ay - by_) / scale


def association_cost(px: float, py: float, pw: float, ph: float,
                     dx_: float, dy_: float, dw: float, dh: float) -> float:
    """Cost of calling a detection the continuation of a predicted track.

    Four terms, normalised so the result is dimensionless and scale-free:

        dx / scale      displacement across the frame, in object heights
        dy / scale
        w ratio - 1     a change of apparent size is itself evidence of
        h ratio - 1     being a DIFFERENT object, not just a moved one

    `scale` is the mean of the two heights, matching the temporal gate this
    grew out of, so the measured 1.5-height operating point stays comparable.
    """
    scale = max(1e-6, (ph + dh) / 2.0)
    ndx = (dx_ - px) / scale
    ndy = (dy_ - py) / scale
    wr = max(pw, dw) / max(1e-6, min(pw, dw)) - 1.0
    hr = max(ph, dh) / max(1e-6, min(ph, dh)) - 1.0
    return math.sqrt(ndx * ndx + ndy * ndy + wr * wr + hr * hr)


class ObjectTracker:
    """Per-camera, per-domain object identity.

    State is keyed (camera, domain) so a person can never inherit a vehicle's
    identity and camera A can never speak for camera B — both properties the
    temporal gate's tests already pinned, and both carried over here.
    """

    def __init__(self, *, enabled: bool = True,
                 max_association_cost: float = 1.75,
                 velocity_alpha: float = 0.5,
                 confirm_after_seconds: float = 1.0,
                 max_age_seconds: float = 3.0,
                 stationary_after_seconds: float = 4.0,
                 stationary_distance: float = 0.15,
                 max_tracks_per_key: int = 64,
                 reassociate_within_seconds: float = 2.0,
                 reassociate_iou: float = 0.3,
                 reassociate_distance: float = 1.0) -> None:
        self.enabled = enabled
        self.max_cost = max_association_cost
        self.alpha = min(1.0, max(0.0, velocity_alpha))
        self.confirm_after = confirm_after_seconds
        self.max_age = max_age_seconds
        self.stationary_after = stationary_after_seconds
        self.stationary_distance = stationary_distance
        self.max_tracks_per_key = max_tracks_per_key
        #: How long a dead identity is kept for, and how close a new detection
        #: has to be to inherit it. 0 seconds switches re-association off.
        self.reassociate_within = max(0.0, reassociate_within_seconds)
        self.reassociate_iou = reassociate_iou
        self.reassociate_distance = reassociate_distance

        self._tracks: dict[tuple[str, str], list[Track]] = {}
        #: Recently dead identities, per (camera, domain).
        self._lost: dict[tuple[str, str], list[_Lost]] = {}
        #: Retirements not yet announced. Held for the re-association window so
        #: a consumer does not throw away an activity's state for an object that
        #: is about to come back - that discarded state is what turned one
        #: person into a second violation.
        self._pending_expiry: list[tuple[float, str, str]] = []
        #: Lifetimes of finished tracks, for the percentiles on /health.
        self._lifetimes: deque = deque(maxlen=500)
        self._now: float = 0.0
        #: Each camera's latest frame time. One tracker serves every camera
        #: and their clocks differ by seconds (a camera catching up after a
        #: stall runs behind), so a camera's retirements are timed on ITS clock.
        self._now_by_camera: dict[str, float] = {}
        #: Per-camera epoch, bumped on reset. Combined with a per-instance
        #: nonce this makes a track id unambiguous across a reconnect, a
        #: hibernation (which builds a new pipeline, hence a new tracker) and a
        #: process restart — see _new_id.
        self._epoch: dict[str, int] = {}
        self._nonce = uuid.uuid4().hex[:6]
        self._counter = 0
        self._expired: list[str] = []

        self.tracks_created = 0
        self.tracks_confirmed = 0
        self.tracks_expired = 0
        self.associations = 0
        self.association_failures = 0
        #: A new detection that inherited the identity of a track which had just
        #: died in the same place.
        self.reassociations = 0
        #: Queued retirement announcements withdrawn because the id came back.
        self.retirements_cancelled = 0
        #: Revivals of an id whose retirement had ALREADY been announced -
        #: consumers had wiped its state. A bug indicator: should stay 0.
        self.revivals_already_announced = 0
        #: A new track created next to one that had just died, which the
        #: thresholds refused. Every one of these is a probable identity switch,
        #: and the number to watch when tuning them.
        self.id_switch_suspects = 0

    # ── identity ─────────────────────────────────────────────────────────────
    def _new_id(self, camera: str) -> str:
        """`camera:nonce.epoch:n` — unique for the life of the deployment.

        The nonce changes whenever this object is built, which is every process
        start and every wake from hibernation; the epoch changes on reconnect.
        Without both, a stored tracker_id would silently mean two different
        objects on either side of a restart, which is worse than storing
        nothing.
        """
        self._counter += 1
        return f"{camera}:{self._nonce}.{self._epoch.get(camera, 0)}:{self._counter}"

    # ── the frame update ─────────────────────────────────────────────────────
    def update(self, camera: str, detections: Sequence, ts: float
               ) -> list[TrackedDetection]:
        """Associate one frame's detections and return them with their tracks.

        Every detection comes back — the tracker never drops anything. Dropping
        is the indexing policy's decision, and keeping them separate is what
        lets suppression be switched off without losing identity.
        """
        self._now = ts
        self._now_by_camera[camera] = ts
        if not detections:
            self._age_out(camera, ts)
            return []
        if not self.enabled:
            # Identity off: every detection is its own single-hit track, so the
            # pipeline downstream needs no special case.
            out = []
            for det in detections:
                cx, by, w, h = _geometry(det.xyxy)
                t = Track(track_id=self._new_id(camera), domain=det.domain,
                          state=ACTIVE, first_seen=ts, last_seen=ts,
                          cx=cx, by=by, w=w, h=h, still_cx=cx, still_by=by,
                          still_since=ts)
                out.append(TrackedDetection(det, t, True))
            return out

        by_domain: dict[str, list] = {}
        for det in detections:
            by_domain.setdefault(det.domain, []).append(det)

        results: list[TrackedDetection] = []
        for domain, dets in by_domain.items():
            results.extend(self._update_domain(camera, domain, dets, ts))
        self._age_out(camera, ts)
        return results

    def _update_domain(self, camera: str, domain: str, dets: Sequence,
                       ts: float) -> list[TrackedDetection]:
        key = (camera, domain)
        tracks = self._tracks.setdefault(key, [])
        geom = [_geometry(d.xyxy) for d in dets]

        # ── candidate pairs under the threshold ──────────────────────────────
        pairs: list[tuple[float, int, int]] = []
        for ti, track in enumerate(tracks):
            px, py = track.predict(ts)
            for di, (cx, by, w, h) in enumerate(geom):
                cost = association_cost(px, py, track.w, track.h, cx, by, w, h)
                if cost <= self.max_cost:
                    pairs.append((cost, di, ti))
        pairs.sort(key=lambda p: p[0])

        # ── greedy one-to-one, cheapest first ────────────────────────────────
        # THE INVARIANT: one track may claim at most one detection from a
        # frame. Two detections in the same frame are certainly two different
        # objects, so letting a track absorb both would merge two identities —
        # the failure mode that made ByteTrack unusable here. Carried over from
        # the temporal gate, where it is already covered by tests.
        taken_det: set[int] = set()
        taken_track: set[int] = set()
        matched: dict[int, int] = {}
        for cost, di, ti in pairs:
            if di in taken_det or ti in taken_track:
                continue
            taken_det.add(di)
            taken_track.add(ti)
            matched[di] = ti
        self.associations += len(matched)
        self.association_failures += len(dets) - len(matched)

        out: list[TrackedDetection] = []
        for di, det in enumerate(dets):
            cx, by, w, h = geom[di]
            ti = matched.get(di)
            if ti is None:
                revived = self._revive(key, cx, by, w, h, ts)
                if revived is not None:
                    # The same object, back after a gap the matcher could not
                    # bridge. It keeps its id, its age and its confirmation, so
                    # everything downstream that remembers this object by id -
                    # a zone stay, a parking timer, a crossing - carries on
                    # instead of starting again as a new person.
                    track = Track(
                        track_id=revived.track_id, domain=domain,
                        state=CONFIRMED if revived.confirmed else TENTATIVE,
                        first_seen=revived.first_seen, last_seen=ts,
                        cx=cx, by=by, w=w, h=h,
                        still_cx=cx, still_by=by, still_since=ts,
                        hits=revived.hits + 1)
                    tracks.append(track)
                    out.append(TrackedDetection(det, track, False))
                    continue
                # confirm_after <= 0 means "trust it on sight", so say so at
                # creation rather than waiting for a second detection that a
                # brief object may never produce.
                born = CONFIRMED if self.confirm_after <= 0 else TENTATIVE
                track = Track(track_id=self._new_id(camera), domain=domain,
                              state=born, first_seen=ts, last_seen=ts,
                              cx=cx, by=by, w=w, h=h,
                              still_cx=cx, still_by=by, still_since=ts)
                if born == CONFIRMED:
                    self.tracks_confirmed += 1
                tracks.append(track)
                self.tracks_created += 1
                out.append(TrackedDetection(det, track, True))
                continue
            track = tracks[ti]
            self._advance(track, cx, by, w, h, ts)
            out.append(TrackedDetection(det, track, False))

        if len(tracks) > self.max_tracks_per_key:
            # Bound the state even if a camera somehow produces hundreds of
            # simultaneous detections. Oldest-first: a track that has not been
            # seen for longest is the safest to forget.
            tracks.sort(key=lambda t: t.last_seen)
            for dead in tracks[:-self.max_tracks_per_key]:
                self._retire(key, dead, ts)
            self._tracks[key] = tracks[-self.max_tracks_per_key:]
        return out

    def _revive(self, key: tuple[str, str], cx: float, by: float, w: float,
                h: float, ts: float) -> Optional[_Lost]:
        """The identity of a track that died where this detection just appeared.

        Deliberately strict: a short window and a tight overlap, because the one
        thing worse than an extra identity is two people sharing one. A near
        miss is counted as a suspected id switch rather than silently accepted.
        """
        pool = self._lost.get(key)
        if not pool or self.reassociate_within <= 0:
            return None
        box = _box_of(cx, by, w, h)
        best, best_score, best_at = None, 0.0, -1
        near_miss = False
        for i, lost in enumerate(pool):
            if ts - lost.retired_at > self.reassociate_within:
                continue
            overlap = _iou(lost.box, box)
            gap = _centre_gap_in_heights(lost.box, box)
            if overlap >= self.reassociate_iou or gap <= self.reassociate_distance:
                score = overlap + max(0.0, 1.0 - gap)
                if score > best_score:
                    best, best_score, best_at = lost, score, i
            elif gap <= self.reassociate_distance * 3.0:
                near_miss = True
        if best is None:
            if near_miss:
                self.id_switch_suspects += 1
            return None
        pool.pop(best_at)
        # Cancel the retirement announcement queued when it died. Left in the
        # queue, it went out anyway once the window passed - telling every
        # consumer to forget an object that was alive again, which wiped a
        # Restricted Zone stay and its cooldown for a person still standing in
        # the zone, and raised a second violation on the next frame.
        waiting = len(self._pending_expiry)
        self._pending_expiry = [e for e in self._pending_expiry if e[2] != best.track_id]
        cancelled = waiting - len(self._pending_expiry)
        self.retirements_cancelled += cancelled
        if not cancelled:
            self.revivals_already_announced += 1
        self.reassociations += 1
        return best

    def _retire(self, key: tuple[str, str], track: Track, ts: float) -> None:
        """Take a track out of service: remember it, and queue the announcement."""
        self._lifetimes.append(max(0.0, track.last_seen - track.first_seen))
        self.tracks_expired += 1
        if self.reassociate_within > 0:
            pool = self._lost.setdefault(key, [])
            pool.append(_Lost(track_id=track.track_id, first_seen=track.first_seen,
                              last_seen=track.last_seen, retired_at=ts,
                              box=_box_of(track.cx, track.by, track.w, track.h),
                              confirmed=track.confirmed, hits=track.hits))
            # Bounded: a camera that churns should not grow this without end.
            if len(pool) > self.max_tracks_per_key:
                del pool[:-self.max_tracks_per_key]
        self._pending_expiry.append((ts, key[0], track.track_id))

    def _advance(self, track: Track, cx: float, by: float, w: float, h: float,
                 ts: float) -> None:
        dt = ts - track.last_seen
        if dt > 0:
            # Exponential smoothing, in units per SECOND. Dividing by the real
            # elapsed time rather than assuming one frame is what makes this
            # survive a change of sample rate, a dropped frame, or a queue
            # backlog without re-tuning.
            inst_vx = (cx - track.cx) / dt
            inst_vy = (by - track.by) / dt
            track.vx = self.alpha * inst_vx + (1.0 - self.alpha) * track.vx
            track.vy = self.alpha * inst_vy + (1.0 - self.alpha) * track.vy

        track.cx, track.by, track.w, track.h = cx, by, w, h
        track.last_seen = ts
        track.hits += 1

        if not track.confirmed:
            if (ts - track.first_seen) >= self.confirm_after:
                # CONFIRMED is entered here and left on the NEXT update, so it
                # is observable for exactly one frame. Movement classification
                # needs a baseline to measure against and this frame is that
                # baseline, not a sample of it.
                track.state = CONFIRMED
                self.tracks_confirmed += 1
                track.still_cx, track.still_by, track.still_since = cx, by, ts
            return

        # Stationary classification, measured from the last position that
        # counted as movement rather than from the previous frame — otherwise a
        # slow walk reads as a sequence of tiny stillnesses and never leaves
        # STATIONARY.
        scale = max(1e-6, track.h)
        moved = math.hypot(cx - track.still_cx, by - track.still_by) / scale
        if moved >= self.stationary_distance:
            track.still_cx, track.still_by, track.still_since = cx, by, ts
            track.state = ACTIVE
        elif (ts - track.still_since) >= self.stationary_after:
            track.state = STATIONARY
        elif track.state == CONFIRMED:
            track.state = ACTIVE

    def _age_out(self, camera: str, ts: float) -> None:
        """Mark unseen tracks LOST, and expire the ones past max_age.

        Expiry is short on purpose. A wrong re-association merges two people
        into one identity, and for search that is worse than an extra track:
        a merged identity is a person who never comes back as a hit.
        """
        for (cam, _domain), tracks in list(self._tracks.items()):
            if cam != camera:
                continue
            keep: list[Track] = []
            for track in tracks:
                idle = ts - track.last_seen
                if idle > self.max_age:
                    self._retire((cam, _domain), track, ts)
                    continue
                if idle > 0 and track.confirmed:
                    track.state = LOST
                keep.append(track)
            self._tracks[(cam, _domain)] = keep
        # Forget dead identities once they are too old to come back.
        for key in [k for k in self._lost if k[0] == camera]:
            fresh = [l for l in self._lost[key]
                     if ts - l.retired_at <= self.reassociate_within]
            if fresh:
                self._lost[key] = fresh
            else:
                del self._lost[key]

    # ── lifecycle boundaries ─────────────────────────────────────────────────
    def reset(self, camera: str) -> None:
        """Drop identity for one camera and start a new id epoch.

        Called on stream reconnect. The frames either side of a reconnect are
        separated by however long the outage lasted, so associating across it
        would be guesswork dressed as continuity.
        """
        self._epoch[camera] = self._epoch.get(camera, 0) + 1
        for key in [k for k in self._tracks if k[0] == camera]:
            for track in self._tracks.pop(key, []):
                self._expired.append(track.track_id)
                self.tracks_expired += 1
                self._lifetimes.append(max(0.0, track.last_seen - track.first_seen))
        # Nothing may be revived across a reconnect - the frames either side are
        # separated by however long the outage lasted - and anything already
        # waiting to be announced is announced now.
        for key in [k for k in self._lost if k[0] == camera]:
            del self._lost[key]
        waiting = [(at, cam, tid) for at, cam, tid in self._pending_expiry if cam != camera]
        self._expired.extend(tid for at, cam, tid in self._pending_expiry if cam == camera)
        self._pending_expiry = waiting

    def forget(self, camera: str) -> None:
        """Camera removed from the index entirely."""
        self._now_by_camera.pop(camera, None)
        self.reset(camera)
        self._epoch.pop(camera, None)

    def drain_expired(self) -> list[str]:
        """Track ids retired since the last call, for the consumers to prune.

        HELD FOR THE RE-ASSOCIATION WINDOW. Announcing a retirement is what makes
        a consumer throw away everything it knows about that object - the zone
        stay, the parking timer, the half-finished crossing - and that discarded
        state is exactly what turned one person standing still into a second
        violation. So an id is announced only once it is too old to be revived.
        A reset or a removal still flushes immediately: there, identity really
        is gone.
        """
        ready = self._expired
        self._expired = []
        if self.reassociate_within <= 0:
            ready.extend(tid for _, _, tid in self._pending_expiry)
            self._pending_expiry = []
            return ready
        keep: list[tuple[float, str, str]] = []
        for at, cam, tid in self._pending_expiry:
            # On the camera's OWN clock: another camera's frames can be seconds
            # ahead, and judging by them announced this one's retirements while
            # its tracks could still come back. A camera that has fallen far
            # behind every other one has stopped sending; its ids are gone.
            own = self._now_by_camera.get(cam, self._now)
            if (own - at >= self.reassociate_within
                    or self._now - at >= self.reassociate_within + STALE_CAMERA_SECONDS):
                ready.append(tid)
            else:
                keep.append((at, cam, tid))
        self._pending_expiry = keep
        return ready

    # ── observability ────────────────────────────────────────────────────────
    def live_tracks(self) -> int:
        return sum(len(v) for v in self._tracks.values())

    def snapshot(self) -> dict:
        states: dict[str, int] = {}
        for tracks in self._tracks.values():
            for t in tracks:
                states[t.state] = states.get(t.state, 0) + 1
        attempts = self.associations + self.association_failures
        lifetimes = sorted(self._lifetimes)

        def life(q: float):
            if not lifetimes:
                return None
            return round(lifetimes[min(len(lifetimes) - 1, int(q * len(lifetimes)))], 2)

        lost_now, oldest_lost = 0, 0.0
        for tracks in self._tracks.values():
            for track in tracks:
                if track.state == LOST:
                    lost_now += 1
                    oldest_lost = max(oldest_lost, self._now - track.last_seen)
        return {
            "enabled": self.enabled,
            "max_association_cost": self.max_cost,
            "max_age_seconds": self.max_age,
            "confirm_after_seconds": self.confirm_after,
            "live_tracks": self.live_tracks(),
            "by_state": states,
            "tracks_created": self.tracks_created,
            "tracks_confirmed": self.tracks_confirmed,
            "tracks_expired": self.tracks_expired,
            # ── identity health ────────────────────────────────────────────
            # A detection that inherited a just-dead identity, and a new track
            # created beside one the thresholds refused to revive. The second
            # number is the probable id switches: watch it when tuning.
            "reassociations": self.reassociations,
            "id_switch_suspects": self.id_switch_suspects,
            "reassociate_within_seconds": self.reassociate_within,
            "identities_remembered": sum(len(v) for v in self._lost.values()),
            "retirements_pending": len(self._pending_expiry),
            "retirements_cancelled": self.retirements_cancelled,
            "revivals_already_announced": self.revivals_already_announced,
            # How long tracks actually live, which is what fragmentation costs.
            "track_life_p50_s": life(0.5),
            "track_life_p95_s": life(0.95),
            "tracks_measured": len(lifetimes),
            # Tracks currently carried without a detection, and the longest of
            # them: how much of what you see is prediction rather than evidence.
            "lost_now": lost_now,
            "longest_unseen_s": round(oldest_lost, 2) if lost_now else 0.0,
            "last_update_ts": self._now or None,
            # A detection that matched no existing track. NOT an error — a new
            # object entering frame is the common cause — but a rate that
            # climbs means association is failing and identities are
            # fragmenting, which is the number to watch after a tuning change.
            "associations": self.associations,
            "association_failures": self.association_failures,
            "association_failure_rate": (
                round(self.association_failures / attempts, 4) if attempts else 0.0
            ),
        }
