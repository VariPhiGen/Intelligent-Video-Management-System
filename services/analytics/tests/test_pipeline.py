"""What this service decides, and where it stops.

THE SEAM IS THE POINT OF THESE TESTS. This service detects, tracks and decides;
it produces crops and metadata and nothing else. If it ever grows an encoder, a
database or a query, the split has failed and `test_the_seam_holds` says so.

The decision logic itself (tracker, indexing policy, motion gate) is covered by
the modules' own suites, which moved here with them. These cover the pipeline
that wires them together.

Run: python3 -m pytest tests -q   (from services/analytics)
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics.config import AppConfig                              # noqa: E402
from analytics.detector import Detection                            # noqa: E402
from analytics.pipeline import DetectionPipeline                    # noqa: E402


class FakeDetector:
    """Returns whatever it was told to, so the pipeline is what is tested."""

    def __init__(self, detections=None) -> None:
        self._d = detections or []
        self.calls = 0

    def detect(self, frame):
        self.calls += 1
        return list(self._d)


@pytest.fixture
def cfg() -> AppConfig:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return AppConfig.from_yaml(os.path.join(here, "config.yaml"))


def frame(w: int = 640, h: int = 480) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


def notice_motion(regions=(), fraction: float = 0.01) -> dict:
    return {"fraction": fraction, "regions": regions, "verdict": "regions",
            "reason": "test", "baseline_valid": True}


def person(x1=10, y1=10, x2=90, y2=210, conf=0.9) -> Detection:
    return Detection(domain="person", xyxy=(x1, y1, x2, y2),
                     confidence=conf, label="person")


# ── the seam ─────────────────────────────────────────────────────────────────
def test_the_seam_holds():
    """This service must not learn to embed, store or search.

    Those need the CLIP encoder and the database, and keeping them out is the
    entire reason analytics is a separate service. An import is how it would
    creep back, so imports are what this checks.

    NOT a search for the word "open_clip": hardware.py PROBES for it (and
    reports it absent here), and candidates.py declares encoder candidates
    because that registry is hardware-general by design — a candidate is
    declared once for all deployments and then asked whether it is available
    on this one. Neither loads anything. Mentioning a runtime is not using it.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]

    # 1. no module from Smart Search's half of the seam
    forbidden = ("from .store", "from .dedup", "from .embedder",
                 "from .queries", "from .writer", "import psycopg")
    offenders = []
    for path in (root / "analytics").glob("*.py"):
        src = path.read_text(encoding="utf-8")
        for f in forbidden:
            if f in src:
                offenders.append(f"{path.name}: {f}")
    assert not offenders, (
        "analytics reached across the seam into Smart Search's half: "
        + ", ".join(offenders))

    # 2. and the image cannot carry them even by accident
    reqs = (root / "requirements.txt").read_text(encoding="utf-8")
    active = [ln.strip() for ln in reqs.splitlines()
              if ln.strip() and not ln.strip().startswith("#")]
    for dep in ("open_clip_torch", "psycopg"):
        assert not any(ln.startswith(dep) for ln in active), (
            f"{dep} is an analytics dependency; the encoder and the store "
            "belong to Smart Search")


def test_the_encoder_really_is_absent_from_this_image():
    """The probe above reports what is installed. Here that must be nothing:
    if open_clip ever appears in this image, the split has quietly undone
    itself and both services are paying for both model sets again."""
    try:
        import open_clip                                    # noqa: F401
    except ImportError:
        return
    pytest.skip("running inside the smartsearch image, which does carry CLIP")


def test_it_produces_observations_not_rows(cfg):
    """The output is a crop plus metadata handed to a sink. Nothing here
    writes anywhere."""
    got = []
    p = DetectionPipeline(cfg, FakeDetector([person()]),
                          sink=lambda s, c, o, frame=None: got.append((s, c, o)))
    p._process("cam-a", frame(), 1000.0, notice_motion())
    assert len(got) == 1
    slug, crop, obs = got[0]
    assert slug == "cam-a"
    assert crop.shape[0] > 0 and crop.shape[1] > 0
    assert set(obs) >= {"camera", "ts", "domain", "confidence", "label",
                        "bbox", "tracker_id", "reason"}
    # bbox is normalised 0-1, the convention the rest of the product uses
    assert all(0.0 <= v <= 1.0 for v in obs["bbox"])


def test_no_sink_still_decides(cfg):
    """PHASE A. With no sink the service must do all of its work and simply
    produce nothing — that is what makes it safe to run beside the pipeline it
    replaces, and the counter is how an operator sees it working."""
    p = DetectionPipeline(cfg, FakeDetector([person()]), sink=None)
    p._process("cam-a", frame(), 1000.0, notice_motion())
    assert p.observations_produced == 1


# ── one object, one observation ──────────────────────────────────────────────
def body_and_torso() -> list[Detection]:
    """The measured 41577/41578 pair (see test_nested_boxes.py) moved into a
    640x480 frame: a full body, and the torso cut out of it."""
    return [person(305, 183, 407, 415, conf=0.425),
            person(305, 187, 391, 319, conf=0.368)]


def test_a_nested_pair_reaches_the_sink_as_one_observation(cfg):
    """nested_boxes.py is tested on its own; THIS is the seam that uses it.
    Remove the call in _detect and both boxes are tracked and indexed as two
    objects while every test in test_nested_boxes.py still passes."""
    got = []
    p = DetectionPipeline(cfg, FakeDetector(body_and_torso()),
                          sink=lambda s, c, o, frame=None: got.append(o))
    p._process("cam-a", frame(), 1000.0, notice_motion())
    assert len(got) == 1
    _, _, x2, y2 = got[0]["bbox"]
    assert (round(x2 * 640), round(y2 * 480)) == (407, 415)   # the body
    assert p.detections_nested == 1
    assert p.detections_seen == 1


def test_a_zero_threshold_lets_both_boxes_through(cfg):
    """The off switch has to reach the seam, not just the function."""
    cfg.detect.nested_containment = 0.0
    got = []
    p = DetectionPipeline(cfg, FakeDetector(body_and_torso()),
                          sink=lambda s, c, o, frame=None: got.append(o))
    p._process("cam-a", frame(), 1000.0, notice_motion())
    assert len(got) == 2
    assert p.detections_nested == 0


def test_the_snapshot_reports_what_suppression_did(cfg):
    """How an operator comparing on/off sees the difference without a query."""
    p = DetectionPipeline(cfg, FakeDetector(body_and_torso()), sink=None)
    p._process("cam-a", frame(), 1000.0, notice_motion())
    snap = p.snapshot()
    assert snap["detections_nested"] == 1
    assert snap["nested_containment"] == cfg.detect.nested_containment


# ── the gate ─────────────────────────────────────────────────────────────────
def test_a_frame_the_broker_found_no_motion_in_never_reaches_the_detector(cfg):
    det = FakeDetector([person()])
    p = DetectionPipeline(cfg, det, sink=None)
    p._process("cam-a", frame(), 1000.0, notice_motion(regions=None))
    assert det.calls == 0, "the detector ran on a frame with no motion"
    assert p.frames_skipped_no_motion == 1


def test_regions_from_the_notice_crop_the_detector_input(cfg):
    """The broker already said WHERE it moved. Detecting on those regions
    rather than the whole frame is what buys accuracy on distant objects."""
    det = FakeDetector([])
    p = DetectionPipeline(cfg, det, sink=None)
    p._process("cam-a", frame(), 1000.0,
               notice_motion(regions=[[0, 0, 100, 100], [200, 200, 300, 300]]))
    assert det.calls == 2, "expected one detector call per region"


def test_an_empty_region_list_means_the_whole_frame(cfg):
    det = FakeDetector([])
    p = DetectionPipeline(cfg, det, sink=None)
    p._process("cam-a", frame(), 1000.0, notice_motion(regions=[]))
    assert det.calls == 1


def test_region_coordinates_come_back_in_full_frame_space(cfg):
    """Getting this wrong does not crash — it produces a plausible bbox in the
    wrong place, which is worse."""
    got = []
    det = FakeDetector([Detection(domain="person", xyxy=(5, 5, 45, 105),
                                  confidence=0.9, label="person")])
    p = DetectionPipeline(cfg, det, sink=lambda s, c, o, frame=None: got.append(o))
    p._process("cam-a", frame(), 1000.0, notice_motion(regions=[[200, 100, 400, 400]]))
    assert got, "nothing produced"
    x1 = got[0]["bbox"][0] * 640
    assert x1 == pytest.approx(205, abs=1), "region offset was not added back"


# ── filters ──────────────────────────────────────────────────────────────────
def test_a_crop_too_small_to_embed_is_not_tracked_either(cfg):
    p = DetectionPipeline(cfg, FakeDetector([person(0, 0, 8, 8)]), sink=None)
    p._process("cam-a", frame(), 1000.0, notice_motion())
    assert p.crops_too_small == 1
    assert p.observations_produced == 0


def test_a_domain_this_camera_does_not_contribute_is_dropped(cfg):
    p = DetectionPipeline(cfg, FakeDetector([person()]), sink=None)
    p.set_domains("cam-a", ("vehicles",))
    p._process("cam-a", frame(), 1000.0, notice_motion())
    assert p.observations_produced == 0


def test_the_policy_suppresses_a_repeat_of_the_same_object(cfg):
    """First sight is always recorded; a second look from the same place is
    not. This is the saving that keeps one object from becoming one row per
    sampled second."""
    p = DetectionPipeline(cfg, FakeDetector([person()]), sink=None)
    t = 1000.0
    for _ in range(4):
        p._process("cam-a", frame(), t, notice_motion())
        t += 0.5
    assert p.observations_produced == 1, "a stationary object was re-recorded"
    assert p.crops_suppressed_by_policy >= 1


# ── the queue ────────────────────────────────────────────────────────────────
def test_a_full_queue_evicts_the_oldest_frame(cfg):
    """For a forensic index the frame that just arrived is worth more than one
    that has been queued for a minute: both describe the same scene and only
    one of them is current."""
    p = DetectionPipeline(cfg, FakeDetector(), sink=None)
    cap = p._queue.maxsize
    for i in range(cap):
        p.submit("cam-a", frame(8, 8), 1000.0 + i, None)
    assert p._queue.qsize() == cap
    p.submit("cam-a", frame(8, 8), 9999.0, None)
    assert p.frames_evicted == 1
    assert p.frames_dropped == 0
    # the survivor set must contain the NEWEST, not the oldest
    stamps = [p._queue.get_nowait()[2] for _ in range(p._queue.qsize())]
    assert 9999.0 in stamps
    assert 1000.0 not in stamps


def test_backlog_age_is_reported_before_anything_is_lost(cfg):
    """The early warning. A drop count only says the pipeline fell behind once
    it is already too late."""
    p = DetectionPipeline(cfg, FakeDetector(), sink=None)
    snap = p.snapshot()
    assert "backlog_age_s" in snap and "queue_depth" in snap
    assert snap["queue_capacity"] == cfg.detect.queue_size


# ── the whole-frame snapshot, for the feed ───────────────────────────────────
def _decode(jpeg: bytes):
    import cv2
    return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)


def test_the_whole_frame_travels_with_the_crop(cfg):
    """The feed shows the scene with the object's box drawn over it, so the
    frame must reach Smart Search alongside the crop — as a JPEG that decodes
    to the frame's own shape, so the normalised bbox lands where it should."""
    got = []
    p = DetectionPipeline(cfg, FakeDetector([person()]),
                          sink=lambda s, c, o, frame=None: got.append(frame))
    p._process("cam-a", frame(640, 480), 1000.0, notice_motion())
    assert got and got[0], "no frame was sent"
    assert _decode(got[0]).shape[:2] == (480, 640)


def test_a_frame_wider_than_the_limit_is_downscaled_keeping_its_aspect(cfg):
    cfg.snapshot.frame_width = 320
    got = []
    p = DetectionPipeline(cfg, FakeDetector([person()]),
                          sink=lambda s, c, o, frame=None: got.append(frame))
    p._process("cam-a", frame(640, 480), 1000.0, notice_motion())
    assert _decode(got[0]).shape[:2] == (240, 320)


def test_several_objects_in_one_frame_share_one_encode(cfg):
    """Two people in a frame are two rows and ONE picture. Encoding it per
    object would multiply the cost and the disk for nothing."""
    got = []
    det = FakeDetector([person(10, 10, 90, 210), person(300, 10, 380, 210)])
    p = DetectionPipeline(cfg, det,
                          sink=lambda s, c, o, frame=None: got.append(frame))
    p._process("cam-a", frame(), 1000.0, notice_motion())
    assert len(got) == 2
    assert got[0] is got[1], "the same frame was encoded twice"
    assert p.snapshots_encoded == 1


def test_width_zero_sends_no_frame(cfg):
    cfg.snapshot.frame_width = 0
    got = []
    p = DetectionPipeline(cfg, FakeDetector([person()]),
                          sink=lambda s, c, o, frame=None: got.append(frame))
    p._process("cam-a", frame(), 1000.0, notice_motion())
    assert got == [None]


def test_a_frame_whose_detections_are_all_suppressed_costs_no_encode(cfg):
    p = DetectionPipeline(cfg, FakeDetector([person()]),
                          sink=lambda s, c, o, frame=None: None)
    p._process("cam-a", frame(), 1000.0, notice_motion())      # first sight
    p._process("cam-a", frame(), 1000.5, notice_motion())      # suppressed
    assert p.snapshots_encoded == 1


# ── one look per track per interval ─────────────────────────────────────────
def test_a_staying_object_is_recorded_once_per_interval(cfg):
    """The operator's rule, end to end through the real tracker and policy:
    first sight, then one record every 10 s while the object stays. At 5 FPS
    that is 4 records in 30 s, not one per change."""
    p = DetectionPipeline(cfg, FakeDetector([person()]), sink=None)
    for i in range(151):                             # 30 s at 5 FPS
        # i / 5, not i * 0.2: the product accumulates error and a record due
        # at exactly 10.0 s would slip to the next frame.
        p._process("cam-a", frame(), 1000.0 + i / 5, notice_motion())
    assert p.observations_produced == 4, "expected records at 0, 10, 20 and 30 s"


# ── the frame's complete detection result ───────────────────────────────────
def test_every_object_in_the_frame_is_boxed_on_it(cfg):
    """A card shows the frame, so it has to carry every object the frame held —
    not only the one that observation is about. Two people in one frame are two
    rows, one picture, and two boxes on it."""
    got = []
    det = FakeDetector([person(10, 10, 90, 210), person(300, 10, 380, 210)])
    p = DetectionPipeline(cfg, det, sink=lambda s, c, o, frame=None: got.append(o))
    p._process("cam-a", frame(), 1000.0, notice_motion())

    assert len(got) == 2
    sent = {o["tracker_id"] for o in got}
    for obs in got:
        boxes = obs["frame_boxes"]
        assert len(boxes) == 2, "an observation carried only its own object"
        assert {b["tracker_id"] for b in boxes} == sent
        assert all(b["recorded"] for b in boxes)
    assert got[0]["frame_boxes"] is got[1]["frame_boxes"], "the list was built twice"


def test_an_object_in_shot_but_not_recorded_is_still_drawn(cfg):
    """THE REASON THE LIST IS SENT AT ALL. Someone recorded three seconds ago
    writes no row for this frame, and a card drawn from rows alone would leave
    them unmarked in a picture they are plainly standing in."""
    got = []
    det = FakeDetector([person(10, 10, 90, 210)])
    p = DetectionPipeline(cfg, det, sink=lambda s, c, o, frame=None: got.append(o))
    p._process("cam-a", frame(), 1000.0, notice_motion())            # first sight
    det._d = [person(10, 10, 90, 210), person(300, 10, 380, 210)]    # a second arrives
    p._process("cam-a", frame(), 1000.4, notice_motion())

    assert len(got) == 2, "only the new object should have been recorded"
    drawn = {b["tracker_id"]: b["recorded"] for b in got[1]["frame_boxes"]}
    assert len(drawn) == 2
    assert drawn[got[0]["tracker_id"]] is False, "the object in shot was not drawn"
    assert drawn[got[1]["tracker_id"]] is True


def test_a_box_is_normalised_and_named_like_the_row_it_belongs_to(cfg):
    got = []
    p = DetectionPipeline(cfg, FakeDetector([person(64, 48, 128, 240)]),
                          sink=lambda s, c, o, frame=None: got.append(o))
    p._process("cam-a", frame(640, 480), 1000.0, notice_motion())
    box = got[0]["frame_boxes"][0]
    assert box["bbox"] == pytest.approx([0.1, 0.1, 0.2, 0.5])
    assert box["bbox"] == pytest.approx(got[0]["bbox"]), "the box left the row behind"
    assert box["label"] == "person" and box["domain"] == "person"
    assert box["tracker_id"] == got[0]["tracker_id"]


def test_no_picture_means_no_box_list(cfg):
    """Boxes are drawn on the frame. With no frame there is nothing to draw on,
    and the card falls back to the crop, which already IS the box."""
    cfg.snapshot.frame_width = 0
    got = []
    p = DetectionPipeline(cfg, FakeDetector([person()]),
                          sink=lambda s, c, o, frame=None: got.append(o))
    p._process("cam-a", frame(), 1000.0, notice_motion())
    assert "frame_boxes" not in got[0]


def test_a_crowd_does_not_put_an_unbounded_list_on_every_row(cfg):
    from analytics.pipeline import MAX_FRAME_BOXES
    crowd = [person(x, 10, x + 20, 60) for x in range(0, 40 * (MAX_FRAME_BOXES + 5), 40)]
    got = []
    p = DetectionPipeline(cfg, FakeDetector(crowd),
                          sink=lambda s, c, o, frame=None: got.append(o))
    p._process("cam-a", frame(2000, 480), 1000.0, notice_motion())
    assert len(got) == len(crowd), "the cap changed which objects are indexed"
    assert len(got[0]["frame_boxes"]) == MAX_FRAME_BOXES
