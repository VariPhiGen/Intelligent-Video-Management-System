"""The face domain, in the service that produces it.

TWO THINGS ARE PINNED HERE AND THE SECOND IS THE ONE THAT REGRESSES.

The reader itself — thresholds, the size floor, the cap — is ordinary function
testing. What matters more is the WIRING: that a face observation is emitted
only for cameras that asked, that it carries its own vector, that its bbox is in
frame coordinates rather than crop coordinates, and above all that a failure in
face work costs the face and not the person row that was already earned.

The mutation-testing result from the NVR's sub-track audit is why: 21 of 21
mutants against the functions were caught and 0 of 9 against the wiring. A suite
that only drives `FaceReader.read` would let every one of those unplug silently.

Run: python3 -m pytest tests/test_faces.py -q
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analytics.faces import FACE_MODELS, FaceReader         # noqa: E402
from analytics.pipeline import FACE_DOMAIN                  # noqa: E402


# ── stubs ───────────────────────────────────────────────────────────────────

def row(x=10, y=10, w=60, h=60, score=0.95):
    """A YuNet detection row: box, 5 landmarks, score — 15 values."""
    return np.array([x, y, w, h] + [0.0] * 10 + [score], dtype="float32")


class StubDetector:
    def __init__(self, rows):
        self._rows = rows
        self.sizes = []

    def setInputSize(self, size):
        self.sizes.append(size)

    def detect(self, image):
        return 1, (np.array(self._rows) if self._rows else None)


class StubRecogniser:
    def __init__(self, vector=None, raises=False):
        self._vector = vector
        self._raises = raises
        self.aligned_calls = 0

    def alignCrop(self, image, face):
        self.aligned_calls += 1
        if self._raises:
            raise RuntimeError("alignment exploded")
        return np.zeros((112, 112, 3), dtype="uint8")

    def feature(self, aligned):
        v = self._vector if self._vector is not None else np.arange(128, dtype="float32")
        return np.array([v], dtype="float32")


def reader(rows, *, score=0.85, min_width=40, cap=2, recogniser=None,
           model="sface-2021dec"):
    return FaceReader(StubDetector(rows), recogniser or StubRecogniser(),
                      model=FACE_MODELS[model],
                      score_threshold=score, min_width_px=min_width,
                      max_per_crop=cap)


def crop(w=200, h=400):
    """A COLOUR crop, because that is what a daytime camera produces and what
    the reader requires. A flat zeros() array is an IR frame as far as the
    colour gate is concerned — which is correct, and made every test here fail
    the moment the gate landed."""
    img = np.zeros((h, w, 3), dtype="uint8")
    img[:, :, 0] = 40      # B
    img[:, :, 1] = 120     # G
    img[:, :, 2] = 200     # R
    return img


def grey_crop(w=200, h=400, level=110):
    """An IR frame: all three channels equal, which is what the night looks
    like on a camera with the cut filter out."""
    return np.full((h, w, 3), level, dtype="uint8")


# ── the reader ──────────────────────────────────────────────────────────────

def test_a_face_comes_back_embedded_and_normalised():
    faces = reader([row()]).read(crop())
    assert len(faces) == 1
    assert faces[0].embedding.shape == (128,)
    assert abs(float(np.linalg.norm(faces[0].embedding)) - 1.0) < 1e-5, \
        "vectors must be L2-normalised; anything comparing by dot product breaks otherwise"


def test_the_score_threshold_is_applied_and_it_is_the_whole_feature():
    """At YuNet's own 0.6 default, most detections on this footage are the BACK
    of a head — correctly localised and useless to a recogniser. 0.85 is what a
    36/36 hand check supports, so a reader that ignored its threshold would
    quietly fill the index with the backs of heads."""
    assert reader([row(score=0.70)]).read(crop()) == []
    assert len(reader([row(score=0.90)]).read(crop())) == 1


def test_faces_below_the_size_floor_are_rejected_and_counted_apart():
    """A site whose faces are all too small is a camera-placement problem, not
    a model problem. One miss counter cannot say which."""
    r = reader([row(w=20)])
    assert r.read(crop()) == []
    assert r.rejected_small == 1
    assert r.faces_found == 0


def test_only_the_best_faces_are_kept_and_they_come_back_best_first():
    r = reader([row(score=0.90), row(score=0.99), row(score=0.87)], cap=2)
    faces = r.read(crop())
    assert [round(f.score, 2) for f in faces] == [0.99, 0.90]


def test_an_embedding_failure_costs_the_face_and_nothing_else():
    r = reader([row()], recogniser=StubRecogniser(raises=True))
    assert r.read(crop()) == []          # no exception escapes


def test_an_inactive_reader_is_silent_rather_than_broken():
    r = FaceReader(None, None, model=FACE_MODELS["sface-2021dec"],
                   score_threshold=0.85, min_width_px=40, max_per_crop=2)
    assert r.active is False
    assert r.read(crop()) == []


def test_a_crop_too_small_to_hold_a_face_is_not_even_asked():
    det = StubDetector([row()])
    r = FaceReader(det, StubRecogniser(), model=FACE_MODELS["sface-2021dec"],
                   score_threshold=0.85, min_width_px=40, max_per_crop=2)
    assert r.read(crop(10, 10)) == []
    assert det.sizes == [], "the detector was called on a 10x10 crop"
    assert r.skipped_too_small == 1, \
        "the skip was silent: nothing on /health would explain the missing crop"


def test_a_degenerate_crop_is_counted_rather_than_vanishing():
    """None, or a zero-sized crop from a collapsed box, is the same fact as
    "too small" and must land in the same counter. It used to return early with
    no counter at all."""
    r = reader([row()])
    assert r.read(None) == []
    assert r.read(np.zeros((0, 0, 3), dtype=np.uint8)) == []
    assert r.skipped_too_small == 2
    assert r.calls == 0


def test_every_crop_given_to_an_active_reader_is_accounted_for_exactly_once():
    """THE ARITHMETIC IS THE POINT. calls + skipped_greyscale +
    skipped_too_small must equal the number of crops handed in, so that a gap
    between that total and the pipeline's own person_crops_searched means a
    real drop rather than a counter nobody wrote.

    This is the invariant that was broken live: 688 crops searched, 674 calls,
    9 greyscale — and five crops accounted for nowhere."""
    r = reader([row()])
    given = [crop(200, 400),        # a call
             crop(10, 10),          # too small
             grey_crop(200, 400),   # greyscale
             crop(19, 300),         # too small: one side under the anchor
             None,                  # degenerate
             crop(200, 400)]        # a call
    for c in given:
        r.read(c)

    snap = r.snapshot()
    counted = (snap["calls"] + snap["skipped_greyscale"]
               + snap["skipped_too_small"])
    assert counted == len(given), (
        f"{len(given)} crops in, {counted} accounted for — "
        f"{len(given) - counted} vanished")
    assert snap["calls"] == 2
    assert snap["skipped_greyscale"] == 1
    assert snap["skipped_too_small"] == 3


def test_an_ir_frame_is_skipped_before_the_detector_runs():
    """Measured on this appliance: 0 faces in 99 near-greyscale crops against
    11.1% on colour. The detector is not asked, because the answer is known and
    the model is trained on colour."""
    det = StubDetector([row()])
    r = FaceReader(det, StubRecogniser(), model=FACE_MODELS["sface-2021dec"],
                   score_threshold=0.85, min_width_px=40, max_per_crop=2)
    assert r.read(grey_crop()) == []
    assert det.sizes == [], "the detector ran on an IR frame"
    assert r.skipped_greyscale == 1
    # NOT counted as a call that found nothing: a dark camera and a camera that
    # sees no faces are different facts, and /health has to keep them apart.
    assert r.calls == 0


def test_the_colour_gate_can_be_turned_off_for_a_site_that_disagrees():
    det = StubDetector([row()])
    r = FaceReader(det, StubRecogniser(), model=FACE_MODELS["sface-2021dec"],
                   score_threshold=0.85, min_width_px=40, max_per_crop=2,
                   require_colour=False)
    assert len(r.read(grey_crop())) == 1
    assert r.skipped_greyscale == 0


def test_a_daytime_crop_is_not_mistaken_for_an_IR_one():
    """The gate is only free while it never fires on real footage."""
    r = reader([row()])
    assert len(r.read(crop())) == 1
    assert r.skipped_greyscale == 0


# ── the wiring: does an observation actually get produced? ──────────────────

class RecordingSink:
    def __init__(self):
        self.sent = []

    # `frame`: the whole-frame JPEG the pipeline now sends beside each
    # observation for the detections feed. Not what these tests are about.
    def __call__(self, slug, crop_img, obs, frame=None):
        self.sent.append((slug, crop_img, obs))


def build_pipeline(face_reader, sink):
    """The real pipeline, with detection stubbed out around the part under test."""
    from analytics.config import AppConfig
    from analytics.pipeline import DetectionPipeline
    cfg = AppConfig.from_yaml("config.yaml")
    return DetectionPipeline(cfg, detector=object(), plate_reader=None,
                             face_reader=face_reader, sink=sink)


def emit(pipeline, *, wanted, track_id="cam:abc:1", frame=None):
    """Drive _emit directly: it is the seam where a face observation is born."""
    from analytics.detector import Detection, PERSON_DOMAIN
    from types import SimpleNamespace
    frame = frame if frame is not None else np.dstack([
        np.full((1080, 1920), 40, dtype="uint8"),
        np.full((1080, 1920), 120, dtype="uint8"),
        np.full((1080, 1920), 200, dtype="uint8")])
    det = Detection(xyxy=(100, 200, 300, 600), confidence=0.9,
                    label="person", domain=PERSON_DOMAIN)
    track = SimpleNamespace(track_id=track_id, confirmed=True)
    pipeline._emit("cam-a", frame, det, track, 1757404800.0, "new", wanted, 1920, 1080)


def test_a_face_observation_is_emitted_beside_the_person_one():
    sink = RecordingSink()
    p = build_pipeline(reader([row()]), sink)
    emit(p, wanted=("person", FACE_DOMAIN))
    domains = [obs["domain"] for _, _, obs in sink.sent]
    assert domains == ["person", FACE_DOMAIN], \
        "the person row is the baseline and must still be sent first"
    face = sink.sent[1][2]
    assert len(face["embedding"]) == 128
    assert face["face_width_px"] == 60
    assert face["tracker_id"] == "cam:abc:1", "the face must name the person's track"
    # WITHOUT THIS the index cannot tell which vector space the row belongs to.
    # Two 128-dim face models share none, so a fleet upgraded on one side would
    # write rows that INSERT cleanly and rank meaninglessly.
    assert face["embedding_model"] == "sface-2021dec"


def test_no_face_work_happens_for_a_camera_that_did_not_ask():
    """The domain gate, not the model's presence, decides. A loaded reader on a
    camera that indexes only people must produce nothing."""
    sink = RecordingSink()
    r = reader([row()])
    p = build_pipeline(r, sink)
    emit(p, wanted=("person", "vehicles"))
    assert [obs["domain"] for _, _, obs in sink.sent] == ["person"]
    assert r.calls == 0, "the face model ran for a camera that never asked for it"


def test_the_face_bbox_is_in_frame_coordinates_not_crop_coordinates():
    """The face is found INSIDE the person crop, so its offset has to be added
    back. Without that every face lands in the top-left of the frame, and the
    error is invisible until someone draws one on a video."""
    sink = RecordingSink()
    p = build_pipeline(reader([row(x=10, y=20, w=60, h=60)]), sink)
    emit(p, wanted=("person", FACE_DOMAIN))
    face = sink.sent[1][2]
    # person box starts at (100, 200); the face at (10, 20) inside it
    assert face["bbox"][0] == pytest.approx((100 + 10) / 1920)
    assert face["bbox"][1] == pytest.approx((200 + 20) / 1080)
    assert face["bbox"][2] == pytest.approx((100 + 70) / 1920)


def test_a_face_reader_that_throws_does_not_lose_the_person_observation():
    """The person row is already earned by the time face work runs. A reader
    that raises must cost the face alone."""
    class Exploding:
        active = True
        model = "sface-2021dec"

        def read(self, crop_img):
            raise RuntimeError("model exploded")

    sink = RecordingSink()
    p = build_pipeline(Exploding(), sink)
    emit(p, wanted=("person", FACE_DOMAIN))
    assert [obs["domain"] for _, _, obs in sink.sent] == ["person"]


def test_the_stored_crop_is_the_aligned_face_the_vector_describes():
    """What is stored has to be what was embedded. A padded portrait would look
    better in the gallery and mean something else."""
    sink = RecordingSink()
    p = build_pipeline(reader([row()]), sink)
    emit(p, wanted=("person", FACE_DOMAIN))
    _, face_crop, _ = sink.sent[1]
    assert face_crop.shape == (112, 112, 3)


def test_faces_are_counted_apart_from_the_crops_that_yielded_none():
    """~90% of person crops here carry no usable face. A single counter would
    make a working model look broken."""
    sink = RecordingSink()
    p = build_pipeline(reader([]), sink)          # detector finds nothing
    emit(p, wanted=("person", FACE_DOMAIN))
    assert p.face_crops_searched == 1
    assert p.faces_emitted == 0
