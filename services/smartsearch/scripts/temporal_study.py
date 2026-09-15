#!/usr/bin/env python3
"""temporal_study — when is a detection worth indexing again?

THE PROBLEM, FROM THE PIPELINE'S OWN ORDER OF WORK. index/pipeline.py embeds
EVERY surviving detection and only then asks the deduplicator whether to keep
it, so today's dedup saves disk and database rows but never the CLIP call that
dominates the per-detection cost. A person standing in view for a minute is
detected, cropped and embedded sixty times.

WHY THE OBVIOUS FIXES ARE OUT. Measured on this site: consecutive person crops
under 5 s apart embed at cosine 0.69-0.74 against a 0.85 threshold, so only 1 in
241 pairs dedups — lowering the threshold would start merging DIFFERENT people,
which is the worse failure for search. And roughly half of consecutive detection
pairs have ZERO bbox overlap, so IoU association fails too.

WHAT THIS SCRIPT DOES. Builds a detection trace by running the REAL detector
over recorded footage at the production 1 fps, records every detection with its
geometry, its CLIP embedding and two cheap appearance signals, and then replays
candidate suppression policies over that fixed trace. Policies are compared on
the same detections, so the differences between them are the policies.

GROUND TRUTH, SUCH AS IT IS. Nothing here trusts the existing database rows —
they are post-dedup output of an older pipeline. But one label IS reliable and
free: two detections in the SAME FRAME are certainly different objects. That
gives a real different-object distribution to measure any proposed signal
against, which is what stops a policy being tuned into merging people.

    python3 scripts/temporal_study.py --segments 12 --json /out/temporal.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np


# ── the trace ────────────────────────────────────────────────────────────────
@dataclass
class Det:
    """One detection, with everything a policy might want to look at."""
    camera: str
    t: float                       # seconds of stream time, 1 fps grid
    frame_id: int
    domain: str
    label: str
    conf: float
    bbox: tuple[float, float, float, float]   # normalised x1,y1,x2,y2
    embedding: np.ndarray
    #: 8x8 grayscale thumbnail, flattened and normalised. Costs microseconds
    #: against CLIP's tens of milliseconds — the point of testing it is to see
    #: whether a cheap signal can stand in before the expensive one runs.
    thumb: np.ndarray
    #: Coarse HSV histogram, also cheap.
    hist: np.ndarray

    @property
    def cx(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2

    @property
    def cy(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2

    @property
    def w(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def h(self) -> float:
        return self.bbox[3] - self.bbox[1]


def iou(a: Det, b: Det) -> float:
    ix = max(0.0, min(a.bbox[2], b.bbox[2]) - max(a.bbox[0], b.bbox[0]))
    iy = max(0.0, min(a.bbox[3], b.bbox[3]) - max(a.bbox[1], b.bbox[1]))
    inter = ix * iy
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 0 else 0.0


def centre_distance_in_heights(a: Det, b: Det) -> float:
    """Centre-to-centre distance, in units of the object's own height.

    NOT IoU, and that is the point: at 1 fps a walking person's boxes often do
    not overlap at all, but the centres are still only a fraction of a body
    apart. Normalising by height makes the measure scale-free, so a distant
    figure and a near one are judged on the same terms.
    """
    scale = max(1e-6, (a.h + b.h) / 2)
    return math.hypot(a.cx - b.cx, a.cy - b.cy) / scale


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))


def thumb_distance(a: Det, b: Det) -> float:
    """Mean absolute difference of 8x8 thumbnails, 0 = identical."""
    return float(np.mean(np.abs(a.thumb - b.thumb)))


def hist_similarity(a: Det, b: Det) -> float:
    """Histogram intersection, 1 = identical."""
    return float(np.minimum(a.hist, b.hist).sum())


# ── building the trace ───────────────────────────────────────────────────────
def build_trace(segment_count: int, hours: tuple[int, int],
                sample_period: float, max_frames_per_segment: int,
                cameras: tuple[str, ...] = (),
                exclude: tuple[str, ...] = ()) -> list[Det]:
    """Run the production detector over recorded footage on a 1 fps grid.

    Decoded by seeking on STREAM time rather than replayed in real time: the
    trace only needs the same frames the sampler would have seen, and waiting
    an hour to collect an hour of footage would make the study unrepeatable in
    practice.
    """
    import cv2
    from PIL import Image

    from index.config import AppConfig
    from index.detector import build_selected
    from index.embedder import ClipEmbedder
    from index.selection import DetectorSelector
    from replay import select_segments

    config = AppConfig.from_yaml(os.environ.get("SEARCH_CONFIG", "config.yaml"))
    m = config.models
    embedder = ClipEmbedder(m.clip_model, m.clip_pretrained, m.device,
                            m.embed_batch_size)
    detector = build_selected(DetectorSelector(m), m.detector_weights,
                              config.ingest.detect_confidence, m.device,
                              m.square_letterbox)
    min_w = config.ingest.min_crop_width
    min_h = config.ingest.min_crop_height

    segments = select_segments("study", segment_count, hours=hours,
                               cameras=cameras, exclude=exclude)
    if not segments:
        return []
    print(f"  {len(segments)} segments from "
          f"{len({s.camera for s in segments})} cameras", flush=True)

    trace: list[Det] = []
    for seg in segments:
        cap = cv2.VideoCapture(seg.path)
        if not cap.isOpened():
            print(f"  skip (cannot open): {os.path.basename(seg.path)}")
            continue
        try:
            for i in range(max_frames_per_segment):
                # Seek a little early and read forward: HEVC cannot decode a
                # P/B frame without its references, so a bare seek into the
                # middle of a GOP yields garbage. Same approach as
                # scripts/backend_parity.py.
                target = i * sample_period
                cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, target - 1.0) * 1000)
                frame = None
                for _ in range(200):
                    ok, cand = cap.read()
                    if not ok or cand is None:
                        break
                    frame = cand
                    if cap.get(cv2.CAP_PROP_POS_MSEC) >= target * 1000:
                        break
                if frame is None:
                    break

                dets = detector.detect(frame)
                if not dets:
                    continue
                h, w = frame.shape[:2]
                rgb = Image.fromarray(frame[:, :, ::-1])
                crops, keep = [], []
                for d in dets:
                    x1, y1, x2, y2 = d.xyxy
                    if (x2 - x1) < min_w or (y2 - y1) < min_h:
                        continue                       # same filter production uses
                    crops.append(rgb.crop((x1, y1, x2, y2)))
                    keep.append(d)
                if not crops:
                    continue

                vecs = embedder.embed_images(crops)
                for d, crop, vec in zip(keep, crops, vecs):
                    small = np.asarray(crop.convert("L").resize((8, 8)),
                                       dtype=np.float32) / 255.0
                    hsv = cv2.cvtColor(
                        cv2.resize(np.asarray(crop)[:, :, ::-1], (32, 32)),
                        cv2.COLOR_BGR2HSV)
                    hist = cv2.calcHist([hsv], [0, 1], None, [8, 8],
                                        [0, 180, 0, 256]).flatten()
                    total = hist.sum()
                    hist = hist / total if total else hist
                    x1, y1, x2, y2 = d.xyxy
                    trace.append(Det(
                        camera=seg.camera, t=seg.start_epoch + target,
                        frame_id=int(seg.start_epoch) + i,
                        domain=d.domain, label=d.label, conf=float(d.confidence),
                        bbox=(x1 / w, y1 / h, x2 / w, y2 / h),
                        embedding=vec, thumb=small.flatten(), hist=hist))
        finally:
            cap.release()
        print(f"  {os.path.basename(seg.path):<34} trace={len(trace)}", flush=True)
    return trace


# ── what the data says before any policy ─────────────────────────────────────
def characterise(trace: list[Det]) -> dict:
    """Distributions for same-frame (certainly different) versus consecutive
    (possibly continuing) pairs.

    THE ONE RELIABLE LABEL. Two detections in one frame are two different
    objects — no tracker, no threshold and no assumption required. Any signal
    proposed for "is this the same object" has to separate that population from
    the consecutive-frame one, or it is not measuring what it claims to.
    """
    by_key: dict[tuple[str, str], list[Det]] = defaultdict(list)
    for d in trace:
        by_key[(d.camera, d.domain)].append(d)
    for v in by_key.values():
        v.sort(key=lambda d: d.t)

    same_frame: list[dict] = []      # certainly DIFFERENT objects
    consecutive: list[dict] = []     # possibly the SAME object
    for key, dets in by_key.items():
        by_frame: dict[int, list[Det]] = defaultdict(list)
        for d in dets:
            by_frame[d.frame_id].append(d)
        for group in by_frame.values():
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    same_frame.append({
                        "cosine": cosine(a.embedding, b.embedding),
                        "iou": iou(a, b),
                        "centre_h": centre_distance_in_heights(a, b),
                        "thumb": thumb_distance(a, b),
                        "hist": hist_similarity(a, b)})
        frames = sorted(by_frame)
        for f0, f1 in zip(frames, frames[1:]):
            if f1 - f0 > 2:                   # not adjacent in the 1 fps grid
                continue
            for a in by_frame[f0]:
                # Nearest by centre: the most likely continuation, which is the
                # charitable reading for a suppression policy.
                b = min(by_frame[f1], key=lambda x: centre_distance_in_heights(a, x))
                consecutive.append({
                    "cosine": cosine(a.embedding, b.embedding),
                    "iou": iou(a, b),
                    "centre_h": centre_distance_in_heights(a, b),
                    "thumb": thumb_distance(a, b),
                    "hist": hist_similarity(a, b),
                    "dt": (b.t - a.t)})
    return {"same_frame_pairs": same_frame, "consecutive_pairs": consecutive}


def summarise(pairs: list[dict], field_name: str) -> dict:
    xs = sorted(p[field_name] for p in pairs if p.get(field_name) is not None)
    if not xs:
        return {}
    def q(p):
        return round(xs[min(len(xs) - 1, int(len(xs) * p))], 3)
    return {"n": len(xs), "p05": q(0.05), "p25": q(0.25), "p50": q(0.50),
            "p75": q(0.75), "p95": q(0.95),
            "mean": round(sum(xs) / len(xs), 3)}


# ── the candidate policies ───────────────────────────────────────────────────
class Policy:
    name = "base"

    def reset(self) -> None:
        self.state: dict = {}

    def index(self, d: Det) -> bool:
        raise NotImplementedError


class IndexEverything(Policy):
    """What the pipeline does today, minus CLIP dedup. The denominator."""
    name = "A0 index-every-detection"

    def index(self, d: Det) -> bool:
        return True


class FixedTemporal(Policy):
    """1. After indexing a class on a camera, suppress it for N seconds.

    The crudest policy, and the one to beat: it needs no geometry, no
    appearance and no per-object state. Its weakness is equally plain — a
    second person arriving during the window is silently dropped.
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.name = f"1 fixed-temporal {seconds:g}s"
        self.reset()

    def index(self, d: Det) -> bool:
        key = (d.camera, d.domain)
        last = self.state.get(key)
        if last is not None and d.t - last < self.seconds:
            return False
        self.state[key] = d.t
        return True


class TemporalSpatial(Policy):
    """2. Suppress only when a recent detection is spatially a plausible
    continuation of this one.

    Uses centre distance in object heights rather than IoU, because IoU is zero
    for about half of consecutive pairs at 1 fps. Per-object rather than
    per-class, so a new person elsewhere in the frame is still indexed.
    """

    def __init__(self, seconds: float, max_centre_h: float) -> None:
        self.seconds = seconds
        self.max_centre_h = max_centre_h
        self.name = f"2 temporal+spatial {seconds:g}s d<{max_centre_h:g}h"
        self.reset()

    def reset(self) -> None:
        self.state = defaultdict(list)      # (cam, domain) -> [(t, Det)]

    def index(self, d: Det) -> bool:
        key = (d.camera, d.domain)
        recent = [(t, o) for t, o in self.state[key] if d.t - t < self.seconds]
        self.state[key] = recent
        for _t, other in recent:
            if centre_distance_in_heights(d, other) <= self.max_centre_h:
                return False
        self.state[key].append((d.t, d))
        return True


class TemporalAppearance(Policy):
    """3. Spatial continuation OR a cheap appearance match.

    The appearance signal is an 8x8 thumbnail distance, not a model: the whole
    value of testing it is that it costs microseconds and could run BEFORE the
    CLIP call, which is where the money is.
    """

    def __init__(self, seconds: float, max_centre_h: float,
                 max_thumb: float) -> None:
        self.seconds = seconds
        self.max_centre_h = max_centre_h
        self.max_thumb = max_thumb
        self.name = (f"3 temporal+appearance {seconds:g}s "
                     f"d<{max_centre_h:g}h thumb<{max_thumb:g}")
        self.reset()

    def reset(self) -> None:
        self.state = defaultdict(list)

    def index(self, d: Det) -> bool:
        key = (d.camera, d.domain)
        recent = [(t, o) for t, o in self.state[key] if d.t - t < self.seconds]
        self.state[key] = recent
        for _t, other in recent:
            near = centre_distance_in_heights(d, other) <= self.max_centre_h
            similar = thumb_distance(d, other) <= self.max_thumb
            if near or similar:
                return False
        self.state[key].append((d.t, d))
        return True


class EventBased(Policy):
    """4. Index the first appearance, then only on meaningful change.

    An event is a run of detections that keep being spatially plausible
    continuations. Within one event a further index is earned by MOVEMENT past
    a threshold or by a refresh interval elapsing, so a person who walks across
    the frame is indexed several times while one standing still is indexed once.
    The event closes after a gap, and the next appearance starts a new one.
    """

    def __init__(self, gap_seconds: float, max_centre_h: float,
                 move_h: float, refresh_seconds: float) -> None:
        self.gap = gap_seconds
        self.max_centre_h = max_centre_h
        self.move_h = move_h
        self.refresh = refresh_seconds
        self.name = (f"4 event gap{gap_seconds:g}s move>{move_h:g}h "
                     f"refresh{refresh_seconds:g}s")
        self.reset()

    def reset(self) -> None:
        self.events = defaultdict(list)     # (cam,domain) -> [dict]
        self.closed = 0

    def index(self, d: Det) -> bool:
        key = (d.camera, d.domain)
        live = []
        for ev in self.events[key]:
            if d.t - ev["last_t"] <= self.gap:
                live.append(ev)
            else:
                self.closed += 1
        self.events[key] = live

        for ev in live:
            if centre_distance_in_heights(d, ev["last_det"]) <= self.max_centre_h:
                moved = centre_distance_in_heights(d, ev["last_indexed"])
                stale = d.t - ev["last_indexed_t"] >= self.refresh
                ev["last_t"], ev["last_det"] = d.t, d
                if moved >= self.move_h or stale:
                    ev["last_indexed"], ev["last_indexed_t"] = d, d.t
                    return True
                return False
        self.events[key].append({"last_t": d.t, "last_det": d,
                                 "last_indexed": d, "last_indexed_t": d.t})
        return True


class TemporalSpatialAssign(Policy):
    """Temporal + spatial, but each stored entry may suppress AT MOST ONE
    detection per frame.

    THE BUG THIS FIXES. The unrestricted rule lets one indexed entry silence
    every detection within its radius, so two people standing together produce
    one indexed object and one that is never indexed at all — for as long as
    they stand together. Measured on the cam1/3/5 trace, that lost 265
    simultaneously-visible objects (9.8% of all detections), while the
    circular "risky" metric reported zero.

    Matching is greedy nearest-first over the whole frame, which is enough here:
    the alternative (optimal assignment) costs more and only differs when two
    candidate pairs are within a hair of each other, where either answer is
    defensible.

    An entry FOLLOWS the object it matched — its position is updated so the
    next frame compares against where the object now is — but its expiry clock
    is NOT refreshed. That keeps the periodic re-index: an object present for
    minutes is indexed once per window rather than once ever, which is what
    makes the row a "still here at T" record instead of a single sighting.
    """

    def __init__(self, seconds: float, max_centre_h: float) -> None:
        self.seconds = seconds
        self.max_centre_h = max_centre_h
        self.name = f"5 assign {seconds:g}s d<{max_centre_h:g}h"
        self.reset()

    def reset(self) -> None:
        # (camera, domain) -> [ {created, det} ]
        self.state = defaultdict(list)

    def index_frame(self, dets: list[Det]) -> list[bool]:
        if not dets:
            return []
        now = dets[0].t
        out = [True] * len(dets)
        by_domain: dict[str, list[int]] = defaultdict(list)
        for i, d in enumerate(dets):
            by_domain[d.domain].append(i)

        for domain, idxs in by_domain.items():
            key = (dets[idxs[0]].camera, domain)
            entries = [e for e in self.state[key]
                       if now - e["created"] < self.seconds]

            pairs = []
            for i in idxs:
                for j, e in enumerate(entries):
                    dist = centre_distance_in_heights(dets[i], e["det"])
                    if dist <= self.max_centre_h:
                        pairs.append((dist, i, j))
            pairs.sort()

            used_det: set[int] = set()
            used_entry: set[int] = set()
            for dist, i, j in pairs:
                # ONE entry, ONE detection. This single constraint is the whole
                # difference from the rejected policy.
                if i in used_det or j in used_entry:
                    continue
                used_det.add(i)
                used_entry.add(j)
                out[i] = False
                entries[j]["det"] = dets[i]        # follow the object
            for i in idxs:
                if i not in used_det:
                    entries.append({"created": now, "det": dets[i]})
            self.state[key] = entries
        return out


def evaluate(trace: list[Det], policy: Policy) -> dict:
    """Run a policy over the trace and count what it would have done."""
    policy.reset()
    kept, suppressed = [], []

    # GROUPED BY FRAME, because a policy that enforces one-to-one matching can
    # only do so if it sees the whole frame at once. Per-detection policies are
    # driven through the same grouping so every policy sees identical input in
    # an identical order.
    frames: dict[tuple[str, int], list[Det]] = defaultdict(list)
    for d in trace:
        frames[(d.camera, d.frame_id)].append(d)

    for key in sorted(frames, key=lambda k: (k[0], frames[k][0].t)):
        group = frames[key]
        if hasattr(policy, "index_frame"):
            decisions = policy.index_frame(group)
        else:
            decisions = [policy.index(d) for d in group]
        for d, keep_it in zip(group, decisions):
            (kept if keep_it else suppressed).append(d)

    # A suppression is SAFE if the thing it suppressed was plausibly the same
    # object as something already indexed; RISKY if it was far away, because
    # then a second object may have been dropped. Judged by geometry, not by
    # the embedding under test.
    by_key: dict[tuple[str, str], list[Det]] = defaultdict(list)
    for d in kept:
        by_key[(d.camera, d.domain)].append(d)
    risky = 0
    for d in suppressed:
        near = [k for k in by_key[(d.camera, d.domain)]
                if abs(k.t - d.t) <= 30 and centre_distance_in_heights(d, k) <= 2.0]
        if not near:
            risky += 1

    # THE HONEST HARM METRIC. `risky` above asks whether a suppressed detection
    # had a near indexed neighbour, which uses the policy's own spatial
    # assumption and is therefore circular — it reported zero on a trace where
    # 73.5% of certainly-different pairs sit inside the threshold.
    #
    # This asks something the policy cannot define away: was a suppressed
    # detection in the SAME FRAME as one that got indexed? Two boxes in one
    # frame are two objects, so every such suppression is a distinct object
    # that will never be findable in search.
    kept_by_frame = defaultdict(set)
    for d in kept:
        kept_by_frame[(d.camera, d.frame_id)].add(id(d))
    co_present_lost = sum(
        1 for d in suppressed if kept_by_frame.get((d.camera, d.frame_id)))

    # WINDOW COVERAGE — the metric that can compare policies, which
    # co_present_lost cannot. That one only counts suppressions in frames where
    # something was ALSO kept, so a policy that keeps more first-frame
    # detections is charged for more losses purely by having more frames with a
    # keeper. It ranks policies backwards.
    #
    # This asks a question with a floor that does not move: inside a window, the
    # largest number of detections seen SIMULTANEOUSLY is a hard lower bound on
    # how many distinct objects were present — two boxes in one frame are two
    # objects. If the policy indexed fewer than that in the window, the
    # difference is distinct objects that were certainly never indexed.
    window = 30.0
    uncovered = 0
    demand = 0
    by_cam_dom = defaultdict(list)
    for d in trace:
        by_cam_dom[(d.camera, d.domain)].append(d)
    kept_ids = {id(d) for d in kept}
    for key, dets in by_cam_dom.items():
        dets.sort(key=lambda x: x.t)
        if not dets:
            continue
        start = dets[0].t
        while start <= dets[-1].t:
            end = start + window
            chunk = [d for d in dets if start <= d.t < end]
            if chunk:
                per_frame = defaultdict(int)
                for d in chunk:
                    per_frame[d.frame_id] += 1
                need = max(per_frame.values())
                got = sum(1 for d in chunk if id(d) in kept_ids)
                demand += need
                uncovered += max(0, need - got)
            start = end

    return {
        "policy": policy.name,
        "co_present_lost": co_present_lost,
        "co_present_lost_pct": round(100 * co_present_lost / max(1, len(trace)), 1),
        "uncovered_objects": uncovered,
        "uncovered_pct": round(100 * uncovered / max(1, demand), 1),
        "object_demand": demand,
        "indexed": len(kept),
        "suppressed": len(suppressed),
        "reduction_pct": round(100 * len(suppressed) / len(trace), 1) if trace else 0,
        "risky_suppressions": risky,
        "risky_pct": round(100 * risky / max(1, len(suppressed)), 1),
        "by_domain": {
            dom: {
                "total": sum(1 for d in trace if d.domain == dom),
                "indexed": sum(1 for d in kept if d.domain == dom),
            } for dom in sorted({d.domain for d in trace})},
        "by_camera": {
            cam: {
                "total": sum(1 for d in trace if d.camera == cam),
                "indexed": sum(1 for d in kept if d.camera == cam),
            } for cam in sorted({d.camera for d in trace})},
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Temporal indexing policy study")
    ap.add_argument("--segments", type=int, default=12)
    ap.add_argument("--hours", default="15,16",
                    help="local-hour window for source footage")
    ap.add_argument("--frames-per-segment", type=int, default=60)
    ap.add_argument("--sample-period", type=float, default=1.0)
    ap.add_argument("--cameras", default="", help="comma separated allow-list")
    ap.add_argument("--exclude", default="", help="comma separated deny-list")
    ap.add_argument("--cache", default="",
                    help="path to save/reuse the detection trace; a rebuild "
                         "costs minutes and the trace is deterministic")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    a, b = args.hours.split(",")
    hours = (int(a), int(b))

    # A trace is deterministic — same segments, same detector, same frames — so
    # it is worth caching. Rebuilding cost 12 minutes per policy idea, which is
    # the difference between trying three variants and trying one.
    cache = args.cache
    trace = None
    if cache and os.path.exists(cache):
        import pickle
        with open(cache, "rb") as fh:
            trace = pickle.load(fh)
        print(f"loaded cached trace: {len(trace)} detections from {cache}")

    if trace is None:
        print(f"building detection trace ({args.segments} segments, "
              f"{hours[0]:02d}:00-{hours[1]:02d}:00 local)…", flush=True)
        trace = build_trace(args.segments, hours, args.sample_period,
                        args.frames_per_segment,
                        cameras=tuple(c.strip() for c in args.cameras.split(',') if c.strip()),
                        exclude=tuple(c.strip() for c in args.exclude.split(',') if c.strip()))
        if cache and trace:
            import pickle
            with open(cache, "wb") as fh:
                pickle.dump(trace, fh)
            print(f"trace cached to {cache}")
    if not trace:
        print("no detections in the chosen footage", file=sys.stderr)
        return 1

    cams = sorted({d.camera for d in trace})
    print(f"\ntrace: {len(trace)} detections, {len(cams)} cameras, "
          f"{len({d.frame_id for d in trace})} frames with detections")
    for dom in sorted({d.domain for d in trace}):
        print(f"  {dom:<10} {sum(1 for d in trace if d.domain == dom)}")

    # ── distributions ────────────────────────────────────────────────────────
    ch = characterise(trace)
    print("\nSIGNAL SEPARATION — same-frame pairs are CERTAINLY different objects")
    print(f"{'signal':<12}{'same-frame (different)':>34}{'consecutive (maybe same)':>34}")
    for fld in ("cosine", "iou", "centre_h", "thumb", "hist"):
        sf = summarise(ch["same_frame_pairs"], fld)
        cs = summarise(ch["consecutive_pairs"], fld)
        if not sf or not cs:
            continue
        print(f"{fld:<12}"
              f"  n={sf['n']:<5} p05={sf['p05']:<7} p50={sf['p50']:<7} p95={sf['p95']:<7}"
              f"  n={cs['n']:<5} p05={cs['p05']:<7} p50={cs['p50']:<7} p95={cs['p95']:<7}")

    # ── how often would the spatial rule be WRONG? ───────────────────────────
    # The decisive number, and the one the "risky" column cannot give: a
    # same-frame pair is CERTAINLY two different objects, so any such pair
    # closer than the threshold is a case the policy would merge. Measured
    # directly rather than inferred from the policy's own assumption.
    print()
    print("SPATIAL THRESHOLD SWEEP  (centre distance / object height)")
    print(f"{'threshold':>10}{'different objects below it':>28}"
          f"{'same-object pairs below it':>28}")
    sf = [p_["centre_h"] for p_ in ch["same_frame_pairs"]]
    cs = [p_["centre_h"] for p_ in ch["consecutive_pairs"]]
    for thr in (0.25, 0.5, 0.75, 1.0, 1.5, 2.0):
        fp = 100 * sum(1 for x in sf if x <= thr) / max(1, len(sf))
        tp = 100 * sum(1 for x in cs if x <= thr) / max(1, len(cs))
        flag = "   <-- proposed" if abs(thr - 1.5) < 1e-9 else ""
        print(f"{thr:>10.2f}{fp:>27.1f}%{tp:>27.1f}%{flag}")

    # ── policies ─────────────────────────────────────────────────────────────
    policies: list[Policy] = [
        IndexEverything(),
        FixedTemporal(5), FixedTemporal(15), FixedTemporal(30),
        TemporalSpatial(15, 1.0), TemporalSpatial(30, 1.5),
        TemporalAppearance(15, 1.0, 0.10), TemporalAppearance(30, 1.5, 0.10),
        EventBased(5, 1.5, 1.0, 30), EventBased(5, 1.5, 2.0, 60),
        EventBased(10, 2.0, 1.5, 30),
        TemporalSpatialAssign(30, 1.5),
        TemporalSpatialAssign(30, 1.0),
        TemporalSpatialAssign(15, 1.0),
        TemporalSpatialAssign(30, 0.5),
    ]
    print(f"\nPOLICY COMPARISON over {len(trace)} detections")
    print(f"{'policy':<42}{'indexed':>9}{'suppr':>8}{'reduce':>8}{'co-lost':>9}"
          f"{'uncovered':>11}{'uncov%':>9}")
    print("-" * 98)
    results = []
    for p in policies:
        r = evaluate(trace, p)
        results.append(r)
        print(f"{r['policy']:<42}{r['indexed']:>9}{r['suppressed']:>8}"
              f"{r['reduction_pct']:>7.1f}%{r['co_present_lost']:>9}"
              f"{r['uncovered_objects']:>11}{r['uncovered_pct']:>9.1f}%")

    if args.json:
        payload = {
            "trace": {"detections": len(trace), "cameras": cams,
                      "hours": list(hours), "segments": args.segments},
            "distributions": {
                fld: {"same_frame": summarise(ch["same_frame_pairs"], fld),
                      "consecutive": summarise(ch["consecutive_pairs"], fld)}
                for fld in ("cosine", "iou", "centre_h", "thumb", "hist")},
            "policies": results,
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        print(f"\nreport written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
