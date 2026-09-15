#!/usr/bin/env python3
"""backend_parity — does OpenVINO see what Torch sees, on this site's own footage?

DEVELOPMENT-TIME VALIDATION, NOT CALIBRATION. This answers one question once,
for a build: is the exported OpenVINO IR a faithful stand-in for the torch
weights on real recordings? It is not run per installation and it decides
nothing at runtime.

TORCH IS A REFERENCE, NOT GROUND TRUTH. Neither backend is labelled data, so a
disagreement is a case to look at, never automatically a failure. The report
separates matched detections from torch-only and openvino-only ones and leaves
the judgement to a human — the one thing an automated "accuracy gate" cannot do
honestly when it has no labels.

WHERE THE FRAMES COME FROM, AND WHY NOT `/clip`. The NVR stores 60-second
MPEG-TS segments recorded with `-c copy`, so the segments themselves are the
original encoded video. `/clip` looked like the obvious way to reach them and is
the wrong one here: `_BROWSER_SAFE_CODECS` is `{"h264"}`, so H.264 is remuxed
but **HEVC is transcoded to H.264** for browser playback. Measured on this site,
four of five cameras record HEVC — cam1, cam3, cam4 and cam5 — so `/clip` would
have handed back re-encoded video for everything except cam2, and a parity test
run on transcoded frames measures the transcoder as much as the detector.

`/snapshot` is out for the same reason one step further: it re-encodes to JPEG.

So this reads the segment files directly and decodes them with OpenCV. The
frames are then exactly the camera's own pixels at its own resolution and
aspect ratio, which is what the sampler feeds the detector in production.

THE INDEX RESOLVES THE TIMESTAMP, NOT THE FILENAME. `segments.db` holds
(camera, start_epoch, duration, filepath) and start_epoch is UTC, while the
filename it points at is LOCAL time — `cam3-2qyj_20260904_194137.ts` is
2026-09-04T14:11:37Z on a UTC+5:30 box. Parsing filenames would silently sample
footage five and a half hours from the intended moment.

Nothing here modifies a recording or the index: the segment database is opened
read-only, the segment files are only read, and no row is written anywhere.

HOW INTERESTING MOMENTS ARE FOUND. Existing SmartSearch rows give timestamps
where something was once detected — used ONLY as a pointer into six hours of
video, never as an expected answer. Quiet windows are sampled separately from
periods with no rows at all, so the sample is not accidentally all activity.

    python3 scripts/backend_parity.py --frames 60
    python3 scripts/backend_parity.py --cameras cam2-6lpf --frames 40 --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

NVR = os.environ.get("NVR_API_URL", "http://nvr:8009")
SEARCHDB = os.environ.get("SEARCHDB_URL",
                          "postgresql://search:search_secret@searchdb:5432/smartsearch")

#: A matched pair must overlap at least this much to count as the same object.
#: 0.5 is the usual detection-matching convention and is deliberately loose:
#: a few pixels of box drift is absorbed downstream by the min-crop filter and
#: the deduplicator, whereas a MISSED object is not absorbed by anything.
IOU_MATCH = 0.5


# ── finding moments worth looking at ─────────────────────────────────────────
def detection_times(cameras: Optional[list[str]], limit: int) -> dict[str, list[datetime]]:
    """Timestamps where SmartSearch once recorded something, per camera.

    LOCATION HINTS ONLY. These rows were produced by the torch detector on
    live frames; treating them as expected answers would bake one backend's
    behaviour into the test it is supposed to be judged by.
    """
    import psycopg

    where = "where sensor_id = any(%s)" if cameras else ""
    sql = f"""
        select cam, ts from (
            select sensor_id as cam, ts from search_persons
            union all select camera_id as cam, ts from search_vehicles
        ) a {where.replace('sensor_id', 'cam')} order by ts
    """
    out: dict[str, list[datetime]] = {}
    with psycopg.connect(SEARCHDB, connect_timeout=10) as conn:
        rows = conn.execute(sql, (cameras,) if cameras else ()).fetchall()
    for cam, ts in rows:
        out.setdefault(cam, []).append(ts)
    for cam in out:
        # Thin them out: consecutive rows are often the same object seconds
        # apart, and sixty near-identical frames prove less than twenty spread
        # across the day.
        picked, last = [], None
        for ts in out[cam]:
            if last is None or (ts - last).total_seconds() >= 20:
                picked.append(ts)
                last = ts
        out[cam] = picked[:limit]
    return out


def quiet_times(camera: str, busy: list[datetime], span: tuple[datetime, datetime],
                count: int, rng: random.Random) -> list[datetime]:
    """Timestamps at least two minutes from anything that was ever detected.

    A comparison drawn only from moments something was seen would measure the
    backends on activity alone, and say nothing about the far commoner case of
    an empty scene — where a spurious extra detection costs storage and a false
    search hit.
    """
    start, end = span
    if end <= start:
        return []
    busy_epochs = [b.timestamp() for b in busy]
    out: list[datetime] = []
    for _ in range(count * 40):
        if len(out) >= count:
            break
        t = rng.uniform(start.timestamp(), end.timestamp())
        if all(abs(t - b) > 120 for b in busy_epochs):
            out.append(datetime.fromtimestamp(t, tz=timezone.utc))
    return out


def camera_span(camera: str) -> Optional[tuple[datetime, datetime]]:
    with urllib.request.urlopen(f"{NVR}/cameras", timeout=15) as fh:
        payload = json.load(fh)
    for cam in payload.get("cameras", []):
        if cam["name"] == camera and cam.get("earliest") and cam.get("latest"):
            return (datetime.fromisoformat(cam["earliest"]),
                    datetime.fromisoformat(cam["latest"]))
    return None


# ── pulling original video ───────────────────────────────────────────────────
#: The NVR's own segment index, mounted read-only. Resolving a timestamp
#: through it rather than through /clip is what keeps HEVC footage un-transcoded
#: — see the note at the top of this file.
SEGMENT_DB = os.environ.get("NVR_SEGMENT_DB", "/data/nvr/segments.db")


def segment_for(camera: str, when: datetime) -> Optional[tuple[str, float]]:
    """(segment file, seek offset in seconds) covering `when`, or None.

    Read-only against the NVR's index. `start_epoch` is UTC and authoritative;
    the filename it points at is local time and must not be parsed.
    """
    ts = when.astimezone(timezone.utc).timestamp()
    try:
        con = sqlite3.connect(f"file:{SEGMENT_DB}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return None
    try:
        row = con.execute(
            "SELECT filepath, start_epoch FROM segments "
            "WHERE camera = ? AND start_epoch <= ? "
            "AND start_epoch + duration >= ? "
            "ORDER BY start_epoch DESC LIMIT 1",
            (camera, ts, ts),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if not row or not os.path.exists(row[0]):
        return None
    return row[0], max(0.0, ts - row[1])


def frame_at(camera: str, when: datetime):
    """The recorded frame nearest `when`, at its native size. No re-encoding.

    ONE DECODE, SHARED BY BOTH BACKENDS. The array returned here is handed to
    torch and to OpenVINO unchanged, so any difference in what they report is a
    difference between the backends and not between two decodes of the same
    moment. Seeking in MPEG-TS lands on the nearest decodable frame rather than
    an exact millisecond, which is fine: both see whichever frame that is.
    """
    import cv2

    located = segment_for(camera, when)
    if located is None:
        return None
    path, offset = located
    cap = cv2.VideoCapture(path)
    try:
        if not cap.isOpened():
            return None

        # SEEK SHORT, THEN DECODE FORWARD. A direct seek to the target lands
        # mid-GOP, and HEVC cannot decode a P/B frame without the references
        # that precede it — observed as a flood of "PPS id out of range" and
        # "Could not find ref with POC" from the decoder, and a third of
        # samples lost. Landing a couple of seconds early gives the decoder a
        # keyframe to start from; reading forward then arrives at the wanted
        # moment with a correctly reconstructed picture.
        lead_in = 2.0
        start = max(0.0, offset - lead_in)
        if start > 0:
            cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000.0)

        target_ms = offset * 1000.0
        frame = None
        # ~25 fps means the lead-in is about 50 frames; the cap is generous
        # enough for a slow stream and small enough to stay bounded.
        for _ in range(400):
            ok, candidate = cap.read()
            if not ok or candidate is None or candidate.size == 0:
                break
            frame = candidate
            if cap.get(cv2.CAP_PROP_POS_MSEC) >= target_ms:
                break
        if frame is None:
            # Damaged tail: fall back to the segment's opening frame rather
            # than discarding the sample entirely.
            cap.set(cv2.CAP_PROP_POS_MSEC, 0)
            ok, frame = cap.read()
            if not ok or frame is None or frame.size == 0:
                return None
        return frame
    finally:
        cap.release()


# ── comparing ────────────────────────────────────────────────────────────────
def iou(a: tuple, b: tuple) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def match(torch_dets: list, ov_dets: list) -> dict:
    """Greedy best-IoU pairing within the same domain.

    Greedy rather than optimal assignment: at these detection counts the two
    agree, and a simpler rule is easier to trust when reading a disagreement.
    """
    remaining = list(range(len(ov_dets)))
    matched, torch_only = [], []
    for td in torch_dets:
        best, best_iou = None, 0.0
        for j in remaining:
            od = ov_dets[j]
            if od.domain != td.domain:
                continue
            v = iou(td.xyxy, od.xyxy)
            if v > best_iou:
                best, best_iou = j, v
        if best is not None and best_iou >= IOU_MATCH:
            od = ov_dets[best]
            remaining.remove(best)
            matched.append({
                "domain": td.domain, "label": td.label,
                "iou": round(best_iou, 4),
                "torch_conf": round(td.confidence, 4),
                "openvino_conf": round(od.confidence, 4),
                "conf_delta": round(od.confidence - td.confidence, 4),
                "torch_box": list(td.xyxy), "openvino_box": list(od.xyxy),
            })
        else:
            torch_only.append({"domain": td.domain, "label": td.label,
                               "confidence": round(td.confidence, 4),
                               "box": list(td.xyxy)})
    ov_only = [{"domain": ov_dets[j].domain, "label": ov_dets[j].label,
                "confidence": round(ov_dets[j].confidence, 4),
                "box": list(ov_dets[j].xyxy)} for j in remaining]
    return {"matched": matched, "torch_only": torch_only, "openvino_only": ov_only}


@dataclass
class Timing:
    samples: list[float] = field(default_factory=list)

    def add(self, ms: float) -> None:
        self.samples.append(ms)

    def summary(self) -> dict:
        if not self.samples:
            return {}
        s = sorted(self.samples)
        return {
            "n": len(s),
            "p50_ms": round(statistics.median(s), 2),
            "p95_ms": round(s[min(len(s) - 1, int(len(s) * 0.95))], 2),
            "mean_ms": round(statistics.fmean(s), 2),
        }


def build_detectors(config, models_dir: str):
    """One torch detector and one OpenVINO detector, both through the REAL
    interface so preprocessing, class filter and box decoding are identical and
    the only difference under test is the backend."""
    from index import artifacts
    from index.detector import OPENVINO, TORCH, UltralyticsDetector

    m = config.models
    weights = m.detector_weights
    conf = config.ingest.detect_confidence

    t0 = time.monotonic()
    torch_det = UltralyticsDetector(weights, conf, "cpu", m.square_letterbox,
                                    backend=TORCH)
    torch_load = time.monotonic() - t0

    built = artifacts.export("detector", weights, "openvino", models_dir=models_dir)
    if not built.ok or not built.path:
        raise RuntimeError(f"could not build the OpenVINO IR: {built.error}")
    t0 = time.monotonic()
    ov_det = UltralyticsDetector(built.path, conf, "cpu", m.square_letterbox,
                                 backend=OPENVINO)
    ov_load = time.monotonic() - t0
    return torch_det, ov_det, {"torch_load_s": round(torch_load, 2),
                               "openvino_load_s": round(ov_load, 2),
                               "artifact": built.path,
                               "artifact_cached": built.cached}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Torch vs OpenVINO on recorded footage")
    ap.add_argument("--cameras", default="", help="comma separated; default all with rows")
    ap.add_argument("--frames", type=int, default=60, help="total frames to compare")
    ap.add_argument("--quiet-fraction", type=float, default=0.35,
                    help="share of frames drawn from periods with no detections")
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--json", default="", help="write the full report here")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--confidence", type=float, default=None,
                    help="override ingest.detect_confidence for BOTH "
                         "backends. Lowering it is how a near-threshold "
                         "disagreement is told apart from a real miss: if "
                         "the object reappears in both at a lower bar, the "
                         "backends agree about the object and differ only "
                         "about whether it cleared 0.35.")
    args = ap.parse_args(argv)

    from index.config import AppConfig

    rng = random.Random(args.seed)
    config = AppConfig.from_yaml(os.environ.get("SEARCH_CONFIG", "config.yaml"))
    if args.confidence is not None:
        config.ingest.detect_confidence = args.confidence
    cams = [c.strip() for c in args.cameras.split(",") if c.strip()] or None

    print("locating moments from existing SmartSearch rows (hints only)…", flush=True)
    per_cam_busy = detection_times(cams, limit=args.frames)
    if not per_cam_busy:
        print("no SmartSearch rows to locate footage with", file=sys.stderr)
        return 1

    n_quiet = int(args.frames * args.quiet_fraction)
    n_busy = args.frames - n_quiet
    plan: list[tuple[str, datetime, str]] = []
    cameras_ordered = sorted(per_cam_busy, key=lambda c: -len(per_cam_busy[c]))
    for i, cam in enumerate(cameras_ordered):
        share = max(1, round(n_busy * len(per_cam_busy[cam])
                             / sum(len(v) for v in per_cam_busy.values())))
        for ts in per_cam_busy[cam][:share]:
            plan.append((cam, ts, "busy"))
    for cam in cameras_ordered:
        span = camera_span(cam)
        if not span:
            continue
        share = max(1, n_quiet // len(cameras_ordered))
        for ts in quiet_times(cam, per_cam_busy[cam], span, share, rng):
            plan.append((cam, ts, "quiet"))
    rng.shuffle(plan)
    plan = plan[:args.frames]
    print(f"planned {len(plan)} frames across {len(cameras_ordered)} cameras "
          f"({sum(1 for p in plan if p[2]=='busy')} busy / "
          f"{sum(1 for p in plan if p[2]=='quiet')} quiet)")

    scratch = tempfile.mkdtemp(prefix="parity-clips-")
    models_dir = os.environ.get("SEARCH_MODELS_DIR", config.models.models_dir)
    report: dict[str, Any] = {"frames": [], "config": {
        "iou_match": IOU_MATCH, "confidence": config.ingest.detect_confidence,
        "square_letterbox": config.models.square_letterbox,
        "weights": config.models.detector_weights,
    }}
    t_time, o_time = Timing(), Timing()

    try:
        print("loading both detectors…", flush=True)
        torch_det, ov_det, load = build_detectors(config, models_dir)
        report["load"] = load
        print(f"  torch {load['torch_load_s']}s, openvino {load['openvino_load_s']}s "
              f"(artifact cached={load['artifact_cached']})")

        fetched = 0
        for idx, (cam, ts, kind) in enumerate(plan, 1):
            # ONE decode, handed to both backends unchanged.
            frame = frame_at(cam, ts)
            if frame is None:
                continue
            fetched += 1

            if fetched <= args.warmup:
                torch_det.detect(frame)
                ov_det.detect(frame)
                continue

            a = time.monotonic(); td = list(torch_det.detect(frame))
            t_ms = (time.monotonic() - a) * 1000
            a = time.monotonic(); od = list(ov_det.detect(frame))
            o_ms = (time.monotonic() - a) * 1000
            t_time.add(t_ms); o_time.add(o_ms)

            cmp_ = match(td, od)
            report["frames"].append({
                "camera": cam, "timestamp": ts.isoformat(), "kind": kind,
                "resolution": f"{frame.shape[1]}x{frame.shape[0]}",
                "torch_count": len(td), "openvino_count": len(od),
                "torch_ms": round(t_ms, 2), "openvino_ms": round(o_ms, 2),
                **cmp_,
            })
            if idx % 10 == 0:
                print(f"  {idx}/{len(plan)}…", flush=True)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    frames = report["frames"]
    if not frames:
        print("no frames could be extracted", file=sys.stderr)
        return 1

    agreed = sum(1 for f in frames if not f["torch_only"] and not f["openvino_only"])
    matched = [m for f in frames for m in f["matched"]]
    ious = [m["iou"] for m in matched]
    deltas = [m["conf_delta"] for m in matched]
    report["summary"] = {
        "frames": len(frames),
        "frames_fully_agreeing": agreed,
        "agreement_rate": round(agreed / len(frames), 4),
        "torch_detections": sum(f["torch_count"] for f in frames),
        "openvino_detections": sum(f["openvino_count"] for f in frames),
        "matched": len(matched),
        "torch_only": sum(len(f["torch_only"]) for f in frames),
        "openvino_only": sum(len(f["openvino_only"]) for f in frames),
        "iou_mean": round(statistics.fmean(ious), 4) if ious else None,
        "iou_min": round(min(ious), 4) if ious else None,
        "conf_delta_mean": round(statistics.fmean(deltas), 4) if deltas else None,
        "conf_delta_max_abs": round(max(abs(d) for d in deltas), 4) if deltas else None,
        "torch_latency": t_time.summary(),
        "openvino_latency": o_time.summary(),
    }

    s = report["summary"]
    print()
    print("=" * 66)
    print("BACKEND PARITY — torch vs openvino on recorded footage")
    print("=" * 66)
    print(f"  frames compared        {s['frames']}  "
          f"({sum(1 for f in frames if f['kind']=='busy')} busy / "
          f"{sum(1 for f in frames if f['kind']=='quiet')} quiet)")
    print(f"  frames in full agreement {s['frames_fully_agreeing']} "
          f"({s['agreement_rate']*100:.1f}%)")
    print(f"  detections   torch {s['torch_detections']}   "
          f"openvino {s['openvino_detections']}")
    print(f"  matched pairs          {s['matched']}")
    print(f"  torch-only             {s['torch_only']}   <- openvino missed these")
    print(f"  openvino-only          {s['openvino_only']}   <- extra vs torch")
    if ious:
        print(f"  IoU on matched         mean {s['iou_mean']}  min {s['iou_min']}")
        print(f"  confidence delta       mean {s['conf_delta_mean']}  "
              f"max |Δ| {s['conf_delta_max_abs']}")
    print(f"  latency torch          {s['torch_latency']}")
    print(f"  latency openvino       {s['openvino_latency']}")
    print()
    print("Disagreements are cases to INSPECT, not failures: torch is a")
    print("reference implementation, not labelled ground truth.")

    disagreeing = [f for f in frames if f["torch_only"] or f["openvino_only"]]
    if disagreeing:
        print()
        print(f"{len(disagreeing)} frame(s) to inspect:")
        for f in disagreeing[:12]:
            print(f"  {f['camera']} {f['timestamp']} [{f['kind']}] "
                  f"torch={f['torch_count']} ov={f['openvino_count']}")
            for d in f["torch_only"]:
                print(f"      torch-only    {d['domain']:<9} conf={d['confidence']} {d['box']}")
            for d in f["openvino_only"]:
                print(f"      openvino-only {d['domain']:<9} conf={d['confidence']} {d['box']}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        print(f"\nfull report written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
