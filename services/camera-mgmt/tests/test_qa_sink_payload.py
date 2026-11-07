"""The message that leaves the appliance, asserted field by field.

WHY THIS SUITE IS WORTH ITS LENGTH. The consumer is a system we do not own and
cannot change, and almost every way of getting this payload wrong is SILENT
over there: a box at the wrong scale renders as a badly-drawn box, an `imgsz`
split on the wrong character yields nulls in a feature blob nobody reads until
training time, a missing trailing underscore lets a frame-persistence gate
discard the event before it is ever stored. None of those raise, none of them
log, and none of them are visible from this side.

So the assertions below are deliberately literal. They are not restating the
implementation — they are pinning the half-dozen details that the remote
system's source code dictates and that a well-meaning refactor would otherwise
"tidy" into something that still passes a shape check and delivers nothing.

No database. `build_payload` is a pure function of a row and a config, which is
why it is a pure function: the part that has to be exactly right is the part
that can be tested without Postgres, a broker or a bucket.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backend.models import Annotation, QaPublishOutbox, QaSinkConfig
from backend.services import qa_sink

CAPTURED = datetime(2026, 10, 15, 9, 21, 4, tzinfo=timezone.utc)
ANNOTATION_ID = uuid.UUID("3f2504e0-4f89-11d3-9a0c-0305e82c3301")


def _row(**over) -> Annotation:
    row = Annotation(
        id=ANNOTATION_ID, camera_slug="gate-cam", class_key="no_helmet",
        captured_at=CAPTURED,
        bbox_x=192.0, bbox_y=108.0, bbox_w=480.0, bbox_h=270.0,
        frame_width=1920, frame_height=1080,
        image_path="/data/annotations/3f2504e0.jpg")
    for key, value in over.items():
        setattr(row, key, value)
    return row


def _config(**over) -> QaSinkConfig:
    config = QaSinkConfig(
        id=1, enabled=True,
        bootstrap_servers="broker.example:9092",
        kafka_topic="vms-event-consume",
        payload_topic="vms_annotation", severity="low",
        s3_bucket="vms-qa-frames", s3_prefix="annotations/",
        class_map={"no_helmet": "PPE-No Helmet"}, use_tls=False)
    for key, value in over.items():
        setattr(config, key, value)
    return config


def _payload(**over):
    config = _config(**over.pop("config", {}))
    return qa_sink.build_payload(
        _row(**over), config, sensor_id="sensor-7", color="#ff8800",
        url="https://b.s3.eu-west-1.amazonaws.com/annotations/x.jpg",
        labelled_url="https://b.s3.eu-west-1.amazonaws.com/annotations/x_labelled.jpg")


# ── The three rules the remote schema imposes ───────────────────────────────


def test_bbox_is_percentages_of_the_frame_not_pixels():
    """192/1920 is 10%, not 192. Pixels here would draw a box off the image.

    Chosen so every number is a different round percentage: a transposed pair
    (x with y, w with h) changes the result, which a 10/10/10/10 box would not.
    """
    assert _payload()["absolute_bbox"][0]["xywh"] == [10.0, 10.0, 25.0, 25.0]


def test_bbox_percentages_are_not_relative_to_the_wrong_axis():
    """Width divides by frame WIDTH and height by frame HEIGHT.

    On a 16:9 frame a single shared divisor still produces plausible-looking
    numbers, so this uses a square frame of a different size entirely — the
    two axes then disagree unless each uses its own.
    """
    box = _payload(frame_width=960, frame_height=1080)["absolute_bbox"][0]
    assert box["xywh"] == [20.0, 10.0, 50.0, 25.0]


def test_imgsz_separator_is_a_colon():
    """Their README says "1920x1080"; their parser splits on ":". The parser wins.

    An "x" here does not fail — it yields a null width and height in the
    feature blob they persist for model training, discovered months later.
    """
    assert _payload()["imgsz"] == "1920:1080"


def test_tracker_id_segment_is_empty_so_the_persistence_gate_fails_open():
    """The consumer takes the text after the LAST underscore as a tracker id.

    An empty one means "untracked", which skips a gate that suppresses any
    detection seen fewer than three times in thirty seconds. An operator's box
    is seen exactly once, so a non-empty tracker id here would let the gate
    discard it before it is ever persisted — the single highest-consequence
    field in this payload, and the least obvious.
    """
    value = _payload()["datetimestamp_trackerid"]
    assert value.endswith("_")
    assert value.rsplit("_", 1)[1] == ""
    # Still carries the capture time, so the string is readable to a human
    # scanning the remote table.
    assert value.startswith("2026-10-15T09:21:04")


# ── Uniqueness, which is what keeps redelivery from duplicating ─────────────


def test_the_three_unique_columns_all_derive_from_the_annotation_id():
    """`org_img`, `video_url` and `org_img_local` are UNIQUE NOT NULL remotely.

    An annotation has one image and no video, so the unique-ness has to come
    from somewhere — and deriving all three from the id is what turns a
    redelivered Kafka message into a primary-key collision over there instead
    of a second review item for the same box.
    """
    payload = _payload()
    assert payload["id"] == str(ANNOTATION_ID)
    assert str(ANNOTATION_ID) in payload["video_url"]
    assert payload["org_img"].startswith("https://")
    assert payload["org_img_local"].startswith("https://")
    # Two DIFFERENT images, not the same URL twice — the second is the copy
    # with the box burned on, and a unique column would reject a duplicate
    # anyway if they ever collapsed into one value.
    assert payload["org_img"] != payload["org_img_local"]


def test_video_url_is_not_pretending_to_be_fetchable():
    """There is no video. A plausible http:// URL would read as a broken link.

    The scheme is the honest signal that this identifier exists to satisfy a
    NOT NULL UNIQUE constraint and nothing else.
    """
    assert _payload()["video_url"].startswith("vms://annotation/")


def test_image_key_is_deterministic_from_the_id():
    """The URL is frozen into the payload before the object exists.

    That only works if the key the relay later uploads to is computable, not
    generated — otherwise the stored URL and the uploaded object disagree and
    every review item shows a broken image.
    """
    key = qa_sink.image_key(_config(), ANNOTATION_ID)
    assert key == f"annotations/{ANNOTATION_ID.hex}.jpg"
    assert key == qa_sink.image_key(_config(), ANNOTATION_ID)


def test_image_key_honours_an_empty_prefix():
    """Bucket root is a real choice, and must not produce a leading slash."""
    assert qa_sink.image_key(_config(s3_prefix=""), ANNOTATION_ID) == \
        f"{ANNOTATION_ID.hex}.jpg"


# ── Routing fields ──────────────────────────────────────────────────────────


def test_payload_topic_is_carried_not_the_kafka_topic():
    """These are different fields with different jobs. Confusing them is the
    single easiest way to make this integration silently useless."""
    payload = _payload()
    assert payload["topic"] == "vms_annotation"
    assert payload["topic"] != _config().kafka_topic


def test_status_is_labelled_so_it_lands_in_the_review_queue():
    """Not APPROVED. The whole point is that a human looks at it."""
    assert _payload()["status"] == "LABELLED"


# ── The class map ───────────────────────────────────────────────────────────


def test_mapped_class_becomes_the_reviewers_own_vocabulary():
    assert _payload()["absolute_bbox"][0]["subcategory"] == "PPE-No Helmet"


def test_unmapped_class_falls_back_to_the_raw_key_rather_than_vanishing():
    """A cosmetic mismatch must not cost an annotation.

    The reviewer sees `forklift` sitting next to `PPE-No Helmet`, notices, and
    somebody fixes the map. Refusing to publish would hide the box instead.
    """
    box = _payload(class_key="forklift")["absolute_bbox"][0]
    assert box["subcategory"] == "forklift"


def test_blank_map_entry_is_treated_as_no_entry():
    """An operator who clears a field in the form means "unset", not "empty"."""
    config = _config(class_map={"no_helmet": "   "})
    assert qa_sink.subcategory_for(config, "no_helmet") == "no_helmet"


def test_confidence_is_one_because_a_human_drew_it():
    """A detector-like 0.9 would be a fabricated number in somebody's dataset."""
    assert _payload()["absolute_bbox"][0]["confidence"] == 1.0


# ── Degenerate input ────────────────────────────────────────────────────────


def test_a_zero_frame_dimension_does_not_raise():
    """Corrupt metadata must not take down a relay pass.

    The division guard produces nonsense percentages, which is correct: the
    annotation is already nonsense, and a crash here would stall every other
    annotation queued behind it.
    """
    payload = _payload(frame_width=0, frame_height=0)
    assert payload["absolute_bbox"][0]["xywh"][0] == pytest.approx(19200.0)


# ── Publishability ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("missing", ["bootstrap_servers", "kafka_topic",
                                     "s3_bucket"])
def test_incomplete_config_is_not_publishable(missing):
    """Each of these is silent at runtime. None of them should enqueue.

    Filling an outbox that cannot drain converts a configuration mistake into a
    growing backlog of pending rows, which is harder to diagnose than nothing
    happening at all.
    """
    assert not qa_sink.is_publishable(_config(**{missing: ""}))


def test_disabled_config_is_not_publishable():
    assert not qa_sink.is_publishable(_config(enabled=False))


def test_absent_config_is_not_publishable():
    """The state of every appliance that never heard of this feature."""
    assert not qa_sink.is_publishable(None)


def test_complete_enabled_config_is_publishable():
    assert qa_sink.is_publishable(_config())


# ── Retry bookkeeping ───────────────────────────────────────────────────────


def _entry(attempts=0):
    return QaPublishOutbox(
        annotation_id=ANNOTATION_ID, payload={}, state="pending",
        attempts=attempts, created_at=CAPTURED, next_attempt_at=CAPTURED)


def test_backoff_doubles_and_then_stops_doubling():
    """Capped so recovery is prompt once the uplink returns.

    Uncapped doubling reaches hours after a dozen failures, which means an
    outage that ends at noon is not noticed until the evening.
    """
    assert qa_sink._backoff(1) == timedelta(seconds=30)
    assert qa_sink._backoff(2) == timedelta(seconds=60)
    assert qa_sink._backoff(99) == timedelta(seconds=qa_sink._BACKOFF_CAP_SECONDS)


def test_a_failure_widens_the_retry_window():
    entry = _entry()
    now = datetime.now(timezone.utc)
    qa_sink._record_failure(entry, "broker unreachable", now)
    assert entry.attempts == 1
    assert entry.state == "pending"
    assert entry.next_attempt_at > now
    assert "broker unreachable" in entry.last_error


def test_a_row_is_parked_rather_than_dropped_once_it_is_hopeless():
    """`failed` with `last_error` is a question an operator can answer.

    Deleting it would lose the only record that an operator's box was supposed
    to reach review and did not.
    """
    entry = _entry(attempts=qa_sink._MAX_ATTEMPTS - 1)
    qa_sink._record_failure(entry, "still unreachable",
                            datetime.now(timezone.utc))
    assert entry.state == "failed"
    assert entry.last_error


def test_an_enormous_error_is_truncated_before_it_reaches_the_column():
    entry = _entry()
    qa_sink._record_failure(entry, "x" * 50_000, datetime.now(timezone.utc))
    assert len(entry.last_error) <= 2000


# ── The sink's own S3 credentials (migration 054) ───────────────────────────
#
# These exist because the appliance's environment key belongs to the Data
# Collection mirror and its training bucket, and the review bucket is a
# different bucket owned by different people whose objects must be publicly
# readable. The first real deployment of this feature failed on exactly that:
# the environment key had no access at all to the bucket somebody typed into
# the form, and the failure surfaced as an AccessDenied in a relay log.


def test_no_credentials_means_sign_with_the_environment():
    """None is the supported default, not a degraded state.

    Every appliance that worked before these columns existed must keep
    working, and `None` is what makes `_client` fall through to exactly the
    resolution it always used.
    """
    assert qa_sink.credentials_for(_config()) is None


def test_a_full_key_pair_overrides_the_environment():
    from backend.crypto import encrypt
    config = _config(aws_access_key_id="AKIAEXAMPLE",
                     aws_secret_access_key_enc=encrypt("s3cret"),
                     aws_region="ap-south-1")
    creds = qa_sink.credentials_for(config)
    assert creds is not None and creds.complete()
    assert creds.access_key_id == "AKIAEXAMPLE"
    assert creds.secret_access_key == "s3cret"
    assert creds.region == "ap-south-1"


def test_a_key_id_with_no_secret_falls_back_rather_than_signing_blind():
    """Half a credential cannot sign anything.

    Falling back to something that works beats failing on a field somebody
    cleared by accident — and the router refuses to SAVE this state anyway, so
    reaching it at all means the row predates that rule or was edited directly.
    """
    config = _config(aws_access_key_id="AKIAEXAMPLE",
                     aws_secret_access_key_enc=None)
    assert qa_sink.credentials_for(config) is None


def test_a_region_alone_is_still_carried():
    """The bucket may sit in a different region from the appliance default,
    and the region is baked into the hostname a reviewer's browser resolves."""
    creds = qa_sink.credentials_for(_config(aws_region="eu-west-1"))
    assert creds is not None
    assert creds.region == "eu-west-1"
    # Not a usable key pair — it must not be mistaken for one.
    assert not creds.complete()


def test_the_frozen_url_uses_the_sinks_region_not_the_appliances():
    """The URL is written into the payload at enqueue time and never revised.

    Resolving it against the appliance's region would hand reviewers a
    hostname for the wrong region — which answers via redirect at best, and
    some corporate proxies do not follow a redirect on an <img>.
    """
    from backend.services import s3_upload
    url = s3_upload.public_url("review-bucket", "annotations/x.jpg",
                               region="eu-west-1")
    assert url == "https://review-bucket.s3.eu-west-1.amazonaws.com/annotations/x.jpg"


def test_an_explicit_public_base_url_still_wins_over_any_region():
    from backend.services import s3_upload
    assert s3_upload.public_url("b", "k.jpg", "https://cdn.example.com/",
                                region="eu-west-1") == "https://cdn.example.com/k.jpg"


# ── The labelled copy ───────────────────────────────────────────────────────


def test_org_img_local_is_a_full_url_not_a_bare_key():
    """Their own approve path overwrites this column with an S3 URL.

    Putting a bare object key here would make one column mean two different
    things depending on which system wrote it last, and anything downstream
    that treats it as fetchable would break on exactly the rows we wrote.
    """
    value = _payload()["org_img_local"]
    assert value.startswith("https://")
    assert not value.startswith("annotations/")


def test_the_labelled_key_sits_beside_the_original_and_is_deterministic():
    """Both URLs are frozen into the payload before either object exists, so
    the key the relay later uploads to has to be computable, not generated."""
    config = _config()
    original = qa_sink.image_key(config, ANNOTATION_ID)
    labelled = qa_sink.labelled_image_key(config, ANNOTATION_ID)
    assert original == f"annotations/{ANNOTATION_ID.hex}.jpg"
    assert labelled == f"annotations/{ANNOTATION_ID.hex}_labelled.jpg"
    assert labelled != original
    assert labelled == qa_sink.labelled_image_key(config, ANNOTATION_ID)


def test_the_labelled_key_honours_an_empty_prefix():
    assert qa_sink.labelled_image_key(_config(s3_prefix=""), ANNOTATION_ID) == \
        f"{ANNOTATION_ID.hex}_labelled.jpg"
