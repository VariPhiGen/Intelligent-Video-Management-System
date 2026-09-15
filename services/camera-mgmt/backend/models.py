from __future__ import annotations

import random
import re
import string
import uuid
from datetime import date, datetime
from enum import Enum
from typing import Any, Literal, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .db import Base


# ─── ORM Model ────────────────────────────────────────────────────────────────
#
# One row per camera across its WHOLE lifecycle. A network scan inserts rows at
# stage='discovered'; probing/verification advance the stage; promotion sets
# name/slug/rtsp_url and stage='registered'. Only registered rows are cameras
# in the product sense (relayed, health-checked, recorded) — everything
# registry-facing filters on stage='registered' (see CameraStage).
# name/slug/rtsp_url are nullable because pre-registration rows don't have them
# yet; a DB CHECK constraint guarantees registered rows always do.

class Camera(Base):
    __tablename__ = "cameras"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    slug: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, unique=True, index=True)
    rtsp_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # External sensor identifier (e.g. CMM sensor UUID). Not unique — the same
    # sensor UUID can appear on multiple physical servers in the platform.
    sensor_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    health_status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="unknown"
    )
    # Named 'camera_metadata' in Python to avoid shadowing SQLAlchemy's
    # class-level MetaData attribute which is also called 'metadata'.
    camera_metadata: Mapped[dict] = mapped_column(
        "camera_metadata", JSONB, nullable=False, default=dict
    )

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    stage: Mapped[str] = mapped_column(
        String(32), nullable=False, default="registered", index=True
    )
    # Per-camera NVR recording opt-out. Recording happens only when the camera
    # is registered AND enabled AND recording (see services/nvr_client.py).
    recording: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ── Discovery data (how the camera was found; kept after registration) ────
    # ip doubles as the rescan dedupe key. Deliberately NOT unique: multi-channel
    # encoders put several cameras on one IP, and manual/bulk rows may lack it.
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    mac: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    onvif_port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rtsp_port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    vendor: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    firmware: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # List[{profile, token, url_raw, verified}] — NO credentials embedded here.
    rtsp_candidates: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # The camera's low-resolution second stream, if it has a useful one. NULL is
    # a valid, fully-supported state — a camera without one records and plays
    # exactly as before.
    #
    # This is a FIELD on the camera, not a Camera row, and that is the whole
    # design. Every subsystem that enumerates cameras — DeepStream sync, motion,
    # the health monitor, events, permissions, audit — keeps seeing one camera
    # and therefore ignores the sub for free. AI never binds the low-res stream
    # because it never sees it. Only the two sync surfaces that project cameras
    # onto recording names (relay, nvr_client) are taught about it, via
    # services/tracks.py.
    #
    #   {url_raw, codec, width, height, fps, source: onvif|derived|manual,
    #    recording_enabled: bool, verified: bool, probed_at: iso8601}
    #
    # No credentials, same as rtsp_candidates: they are composed at use time.
    sub_track: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    open_ports: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Fernet-encrypted ONVIF/RTSP credentials (see crypto.py) — reusable for
    # re-probe / profile switch after registration; never returned in plaintext.
    enc_username: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    enc_password: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    discovery_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_scanned_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Privacy masks: list of polygons in normalized coords [[[x,y],...],...].
    # Burned into recordings by the NVR (opt-in re-encode); live view untouched.
    privacy_masks: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Recording schedule (Milestone-style calendar). NULL = record 24/7.
    # {"mode": "weekly"|"monthly", "rules": [{"days": [...], "start": "HH:MM",
    # "end": "HH:MM"}]} — see services/schedule.py for semantics. Enforced by
    # the registry→NVR reconcile loop in server-local time; live view untouched.
    recording_schedule: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # Motion detection opt-in (default OFF): the motion service analyses this
    # camera's RELAY stream (services/motion; synced like NVR recording).
    # Sensitivity is a preset name (low/medium/high); NULL = service default.
    motion_detection: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    motion_sensitivity: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # Smart Search indexing opt-OUT. Defaults on, unlike motion_detection:
    # search that silently covers only some cameras is worse than none, because
    # a nil result reads as "never recorded". Turning it off is a deliberate act
    # and the SPA has to say so wherever results are shown.
    search_indexing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Which domains this camera contributes: person / vehicles / plate.
    #: "vehicles" is plural to match the wire contract and the table names.
    search_domains: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=lambda: ["person", "vehicles"]
    )
    # Per-camera NVR retention (days footage is kept before deletion). NULL =
    # appliance default (NVR_DEFAULT_RETENTION_DAYS). Synced to the NVR on
    # change and on every reconcile pass (services/nvr_client.py).
    retention_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Operator's justification for the retention period (DPDP purpose-binding).
    # Free text; the audit log records who/when, this records why.
    retention_justification: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # DPDP lawful basis + purpose. First-class (was free-form metadata) so a
    # camera cannot register without them and the posture report can attest them.
    lawful_basis: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    purpose: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Whether required notice/signage is posted at the camera (Rule 3 notice).
    notice_posted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # STQC / BIS-ER certification posture (mandatory for cameras SOLD in India
    # from 1 April 2026). Records posture, does not gate registration — an
    # existing uncertified fleet is lawful to operate. Default "unknown" is
    # load-bearing: never guess a camera into "certified". See migration 023.
    stqc_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unknown", server_default=text("'unknown'")
    )
    stqc_certificate_no: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    stqc_valid_until: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    # Attestation provenance, kept on the row so the posture report can render
    # "verified by X on Y" without joining the audit chain.
    stqc_verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stqc_verified_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Per-camera groom-after override: days before footage is remuxed to
    # keyframe-only ("cold"). NULL = the NVR's appliance default
    # (recording.groom_after_days in its cameras.yaml). Synced like retention.
    groom_after_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # CMM (Camera Motion Map) analytics config: activity→zone mapping + polygon
    # zones in normalized 0–1 coords. Authored in the Config → Zones & Analytics
    # tab; emitted as a JSON contract for the external DeepStream pipeline (which
    # pulls it — the VMS runs no inference). {} = nothing configured. The shape
    # is fixed by migration 013 and by the tab that authors it.
    analytics_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


# Append-only health-transition log backing uptime/downtime history. The
# health monitor writes one row per ACTUAL status change (not per poll), so
# volume stays proportional to flapping, not fleet size × poll rate. Uptime
# graphs/percentages are reconstructed by replaying these transitions.

class RolePolicy(Base):
    """Editable capability policy per Keycloak realm role (supervisor / operator
    / viewer / dpo). Admin is never stored — it always has full access
    (services/policy.py)."""

    __tablename__ = "role_policies"

    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    policy: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AnalyticsActivityType(Base):
    """The Activity Type catalog: a synced copy of the CPU activity registry
    (analytics GET /activities, see services/activity_catalog.py) plus what an
    administrator owns — label, colour, order. A camera's analytics_config
    activities reference these by `key`. Which activities exist, their zone
    rule and their settings come from code, never from this table's editor."""

    __tablename__ = "analytics_activity_types"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    color: Mapped[str] = mapped_column(String(9), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Per-type DETECTOR parameter schema (list[ParamField]-shaped JSON) — never
    # the schedule (active_hours/active_days stay built-in on every activity).
    # [] means "this type has no extra settings", not "unset" (migration 016).
    params_schema: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # From the registry (migration 036). available | hold | unregistered.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unregistered", server_default="unregistered"
    )
    # optional (no zone → whole frame) | required | tripwire.
    zone_rule: Mapped[str] = mapped_column(
        String(16), nullable=False, default="required", server_default="required"
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    definition_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class MonitorHeartbeat(Base):
    """Single-row dead-man's switch: the health monitor stamps beat_at every
    poll cycle. At startup, a beat far in the past means the service died
    ungracefully — the span since then is backfilled as 'unknown' status
    events so uptime never counts unmonitored time as camera uptime
    (services/health.py: startup_mark_unknown)."""

    __tablename__ = "monitor_heartbeat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    beat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CameraStatusEvent(Base):
    __tablename__ = "camera_status_events"
    __table_args__ = (
        Index("ix_status_events_camera_time", "camera_id", "changed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    camera_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("cameras.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AuditLog(Base):
    """Append-only, hash-chained record of auditable actions (DPDP Rule 6 /
    STQC Level-2). Rows are never updated or deleted — a DB trigger enforces it
    (migration 019). ``entry_hash = sha256(prev_hash || canonical(payload))``
    links each row to the one before it, so tampering is detectable on read.
    Writes go through services/audit.py (advisory-locked append); this class is
    used for reads/verification. ``ts`` is stamped in Python (UTC) so the value
    that was hashed is exactly the value stored."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    source_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="success")
    prev_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)



class Sitemap(Base):
    """Floor-plan image for the Map tab. Bytes live in Postgres — the API
    container has no writable volume — and are served with an ETag from
    /api/sitemaps/{id}/image. Camera placement references sitemaps from
    camera_metadata JSONB; routers/sitemaps.py sweeps that key on delete."""

    __tablename__ = "sitemaps"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    image: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    uploaded_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Georeferencing control points: a list of {x, y, lat, lng} (x/y normalized
    # 0–1) anchoring the image to real-world coordinates, or NULL when the map
    # is uncalibrated. 2 points → similarity transform, 3 → full affine. Lets a
    # camera's GPS auto-place on the plan (see migration 026).
    calibration: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    # HA peripheral placement moved out to sitemap_ha_placements in migration
    # 029 — a real table with foreign keys, so a pin can no longer outlive the
    # device or the plan it points at.


class Peripheral(Base):
    """A physical peripheral in the operator's inventory — a light, a maglock,
    a siren. Only what a person can actually know is writable here; `last_state`
    and `last_seen` belong to a future Home-Assistant bridge and stay NULL
    until one reports, which is what lets the UI say "state unknown" instead of
    presenting a typed-in status as live (migration 029)."""

    __tablename__ = "peripherals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    location: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    vendor: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_state: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    last_seen: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class SitemapHaPlacement(Base):
    """One peripheral pinned to one plan. Composite PK (a device is placed at
    most once per map) and ON DELETE CASCADE both ways — see migration 029."""

    __tablename__ = "sitemap_ha_placements"

    sitemap_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sitemaps.id", ondelete="CASCADE"), primary_key=True,
    )
    peripheral_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("peripherals.id", ondelete="CASCADE"), primary_key=True,
    )
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)



# ─── Event spine (migration 022) ──────────────────────────────────────────────
#
# Two tables, deliberately: a DETECTION is what the pipeline emitted, an ALERT
# is what a human is asked to look at. Compound rules turn several detections
# into one alert (N:1), false-positive reporting needs the detections that never
# alerted, and the two have retention profiles orders of magnitude apart. See
# migration 022's docstring for the full reasoning.

class AnalyticsEvent(Base):
    """One detection from the analytics pipeline.

    Bulk personal data on a short clock: ``expires_at`` is stamped at insert
    from the owning camera's retention policy so each row carries its own
    deletion deadline, and the retention sweep is an indexed
    ``DELETE WHERE expires_at < now()``. Lawful basis is inherited from the
    camera (migration 021), not restated per row.

    ``id`` is supplied by the PRODUCER, not the database — the pipeline delivers
    at-least-once, so a client-minted UUID makes retries idempotent via
    ``ON CONFLICT DO NOTHING``. This is why it deviates from the BigInteger key
    used by CameraStatusEvent / AuditLog.
    """

    __tablename__ = "analytics_events"
    __table_args__ = (
        Index("ix_events_camera_time", "camera_id", text("started_at DESC")),
        Index("ix_events_activity_time", "activity", text("started_at DESC")),
        # Fleet-wide newest-first (the Events tab's live ticker) — migration 035.
        Index("ix_events_started_at", text("started_at DESC"), text("id DESC")),
        Index("ix_events_expires_at", "expires_at"),
        Index("ix_events_attributes", "attributes", postgresql_using="gin"),
        Index(
            "ix_events_dedupe",
            "dedupe_key",
            text("started_at DESC"),
            postgresql_where=text("dedupe_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    camera_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False
    )
    # Soft reference to AnalyticsActivityType.key — NOT a FK. The pipeline emits
    # more activity types than the catalog currently offers, and a detection
    # from an un-catalogued type must be storable rather than rejected.
    activity: Mapped[str] = mapped_column(String(64), nullable=False)
    # Region name from the camera's analytics_config.regions.
    # NULL = whole-frame detector, which is a real config and not "unset".
    zone: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    # An interval, not an instant — dwell/queue/loitering have duration.
    # ended_at NULL = a point event, or one still open.
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 0..1; NULL when the detector reports none (distinct from 0.0).
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Tracker id — unique only within a camera + tracker session, so not a key.
    # This is the seam cross-camera re-identification would join on.
    track_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Coarse class (person|vehicle|…) promoted out of attributes so the common
    # filter is a btree hit rather than a JSONB probe.
    object_class: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Detector attributes verbatim (colour, PPE flags, plate text, …). What
    # compound rules filter on, and the shape we cannot predict — so schemaless.
    attributes: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    snapshot_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Stable key for cooldown collapse — a busy line-crossing emits thousands of
    # rows an hour and the collapse strategy has to predate the data.
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # When it HAPPENED (started_at) vs when we received it. Both, so pipeline lag
    # and camera clock skew stay diagnosable.
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # deepstream|motion|onvif|manual — ONVIF Profile M metadata would land here
    # without a schema change.
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="deepstream", server_default="deepstream"
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Alert(Base):
    """A detection (or several) that matched a rule and needs human attention.

    Small working set with a state machine, in contrast to AnalyticsEvent's
    bulk. ``resolution`` is the column that makes per-rule false-positive
    reporting possible at all — it cannot be reconstructed after the fact, so it
    exists from the first row even though nothing writes it yet.

    ``rule_id`` has no FK until migration 023 brings the rules engine; the rule
    schema depends on how conditions get expressed and guessing it now would
    cost a re-migration. ``rule_name`` is denormalised so a historical alert
    still reads correctly once its rule is renamed or deleted.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_state_time", "state", text("triggered_at DESC")),
        Index("ix_alerts_camera_time", "camera_id", text("triggered_at DESC")),
        Index(
            "ix_alerts_rule_time",
            "rule_id",
            text("triggered_at DESC"),
            postgresql_where=text("rule_id IS NOT NULL"),
        ),
        Index("ix_alerts_event_ids", "event_ids", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    rule_id: Mapped[Optional[uuid.UUID]] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    rule_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    # The detection(s) behind this alert — an array rather than a join table:
    # compound alerts cite a handful of events, never thousands.
    event_ids: Mapped[list] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)),
        nullable=False,
        default=list,
        server_default=text("'{}'::uuid[]"),
    )
    # Denormalised from the triggering event — every operator view filters by
    # camera, and joining through an array to reach it would be absurd.
    camera_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False
    )
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default="info", server_default="info"
    )
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="new", server_default="new"
    )
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acknowledged_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    assigned_to: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # true_positive|false_positive|duplicate|no_action
    resolution: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    resolution_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Verification (VLM or human) — nullable now, populated when verification
    # lands. Cheap to carry; painful to backfill.
    verified_by: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    verdict: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    verdict_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Per-transport delivery receipts, so "was anyone actually told?" is
    # answerable without correlating against service logs.
    notified: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # Evidence lock: while true, neither this alert nor the footage it cites may
    # be aged out. Blocking camera deletion while holds exist is an app-layer
    # check (the FK above still cascades).
    legal_hold: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ─── Enums ────────────────────────────────────────────────────────────────────

class HealthStatus(str, Enum):
    UNKNOWN = "unknown"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"
    DISABLED = "disabled"


class CameraStage(str, Enum):
    """Camera lifecycle. Runtime health lives in health_status — orthogonal:
    'registered but disconnected' must be expressible."""

    DISCOVERED = "discovered"      # found on network, not yet probed with creds
    PROBING = "probing"            # ONVIF probe in progress
    NO_ONVIF = "no_onvif"          # reachable but no usable ONVIF service
    AUTH_FAILED = "auth_failed"    # ONVIF reachable but credentials rejected
    VERIFIED = "verified"          # ONVIF ok + RTSP stream confirmed playable
    REGISTERED = "registered"      # a real camera: relayed, monitored, recorded
    IGNORED = "ignored"            # dismissed by the admin
    UNREACHABLE = "unreachable"    # no open ports


class AlertState(str, Enum):
    """Operator workflow for an alert. Stored as a plain String(16) column (as
    stage/health_status are) rather than a PG enum, so adding a state is a code
    change and not a migration."""

    NEW = "new"                      # raised, nobody has looked
    ACKNOWLEDGED = "acknowledged"    # seen by a human
    ASSIGNED = "assigned"            # owned by someone (assigned_to)
    RESOLVED = "resolved"            # dealt with; resolution says how it landed
    DISMISSED = "dismissed"          # closed without action


class AlertSeverity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AlertResolution(str, Enum):
    """How a resolved alert actually landed. FALSE_POSITIVE is the whole point:
    per-rule false-positive rate is the feedback loop that makes rule tuning
    measurable instead of guesswork, and it cannot be derived after the fact."""

    TRUE_POSITIVE = "true_positive"
    FALSE_POSITIVE = "false_positive"
    DUPLICATE = "duplicate"
    NO_ACTION = "no_action"


class EventSource(str, Enum):
    """Where a detection came from. Kept open-ended on purpose — ONVIF Profile M
    metadata from cameras doing their own analytics would arrive as ONVIF with
    no schema change."""

    CPU = "cpu"                # the analytics service's CPU Activity Engine
    DEEPSTREAM = "deepstream"
    MOTION = "motion"
    ONVIF = "onvif"
    MANUAL = "manual"


# ─── Slug helpers ─────────────────────────────────────────────────────────────

_SLUG_VALID = re.compile(r"^[a-z0-9][a-z0-9\-]{0,253}[a-z0-9]$")


# Recording name suffix for a camera's low-resolution second stream. Reserved:
# a camera slugged `foo_sub` would collide with camera `foo`'s sub track in the
# NVR index and the relay, and the two would overwrite each other's footage.
SUB_TRACK_SUFFIX = "_sub"


def sub_recording_name(slug: str) -> str:
    """The recording/relay name for `slug`'s sub track."""
    return f"{slug}{SUB_TRACK_SUFFIX}"


def owning_slug(recording_name: str) -> str:
    """The CAMERA SLUG a recording name belongs to — inverse of
    :func:`sub_recording_name`. `<slug>_sub` -> `<slug>`; a bare slug unchanged.

    Exists because the two namespaces are easy to confuse and the confusion is
    silent. Per-camera facts (the re-stamp flag, health, backoff) are keyed on
    the SLUG, while the relay and the NVR build one path per TRACK. A surface
    that looks a per-camera fact up by the track name it happens to be building
    reads a key nothing ever writes, and simply behaves as if the fact were
    false — which is how a re-stamped camera kept feeding its sub track from
    the same broken clock.

    Safe against a slug that merely looks like a sub: `generate_slug` always
    ends a slug with `-<4 alnum>`, so no real slug ends in `_sub`.
    """
    if recording_name.endswith(SUB_TRACK_SUFFIX):
        return recording_name[: -len(SUB_TRACK_SUFFIX)]
    return recording_name


def generate_slug(name: str) -> str:
    """Derive a URL-safe slug from a camera name and append a 4-char random suffix."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "camera"
    base = base[:50]  # cap base length
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{base}-{suffix}"


def validate_slug_format(v: str) -> str:
    v = v.strip().lower()
    if len(v) < 2:
        raise ValueError("Slug must be at least 2 characters")
    if not _SLUG_VALID.match(v):
        raise ValueError(
            "Slug must be lowercase alphanumeric with internal hyphens only "
            "(no leading/trailing hyphens)"
        )
    # `_sub` is how a camera's low-resolution track is named in the NVR index
    # and the relay. A camera actually called `foo_sub` would share a recording
    # name with camera `foo`'s sub track, and they would write over each other.
    # (Underscores are already rejected by the pattern above; this is the
    # explicit guard so the reservation survives a future pattern change.)
    if v.endswith(SUB_TRACK_SUFFIX):
        raise ValueError(
            f"Slug must not end with '{SUB_TRACK_SUFFIX}' — that suffix is "
            "reserved for a camera's low-resolution sub track"
        )
    return v


# ─── DPDP lawful basis ────────────────────────────────────────────────────────
#
# The closed set a registered camera's lawful_basis must belong to. Unlike the
# old State/law-enforcement framings, these are DPDP s.7 legitimate uses plus
# consent — including "Employment purposes", the private-sector majority case.
# The onboarding wizard mirrors this list (frontend useDiscovery.ts).

LAWFUL_BASES: tuple[str, ...] = (
    "Consent",
    "Employment purposes",
    "Legal obligation / statutory duty",
    "Public safety / State function",
    "Medical emergency",
    "Court order / legal proceedings",
)


def validate_lawful_basis(lawful_basis: Optional[str], purpose: Optional[str]) -> tuple[str, str]:
    """Enforce DPDP purpose-binding before a camera registers: a lawful basis
    from LAWFUL_BASES and a non-empty purpose. Returns the cleaned pair; raises
    HTTPException(422) otherwise. Called at every stage='registered' entry point."""
    from fastapi import HTTPException  # local import — keep models import-light

    lb = (lawful_basis or "").strip()
    pu = (purpose or "").strip()
    if lb not in LAWFUL_BASES:
        raise HTTPException(
            422,
            detail="A lawful basis is required to register a camera — one of: "
                   + ", ".join(LAWFUL_BASES),
        )
    if not pu:
        raise HTTPException(
            422, detail="A purpose is required to register a camera (why it records)."
        )
    return lb, pu


# ─── STQC / BIS-ER certification posture ──────────────────────────────────────
#
# India requires Essential Requirements / STQC certification for CCTV cameras
# SOLD from 1 April 2026. Unlike LAWFUL_BASES this gates nothing at registration
# (see migration 023) — it is a posture field, so the vocabulary has to be able
# to say "we don't know", and that is the default.

STQC_STATUSES: tuple[str, ...] = (
    "unknown",          # nobody has checked — the honest default, never guessed away
    "certified",        # holds a valid ER/STQC certificate
    "not_certified",    # checked, and it does not
    "exempt",           # outside the mandate's scope (e.g. analogue, non-networked)
    "not_applicable",   # not procured in India / pre-dates the mandate
)

# Certificates expiring inside this window are surfaced as "expiring soon" in
# the posture report, so procurement has a quarter's notice to re-certify.
STQC_EXPIRY_WARN_DAYS: int = 90


def validate_stqc_status(status: Optional[str]) -> str:
    """Normalise an STQC posture value, defaulting to 'unknown'. Raises 422 on a
    value outside STQC_STATUSES. Unset is legal here — deliberately, since this
    field never blocks registration."""
    from fastapi import HTTPException  # local import — keep models import-light

    s = (status or "").strip() or "unknown"
    if s not in STQC_STATUSES:
        raise HTTPException(
            422,
            detail="Unknown STQC status — one of: " + ", ".join(STQC_STATUSES),
        )
    return s



# ─── Pydantic Schemas ─────────────────────────────────────────────────────────

class CameraCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=255)
    rtsp_url: str = Field(min_length=10)
    slug: Optional[str] = Field(default=None, description="Leave blank to auto-generate")
    sensor_id: Optional[uuid.UUID] = Field(default=None, description="External sensor UUID")
    enabled: bool = True
    recording: bool = Field(default=True, description="Record this camera on the NVR")
    metadata: dict[str, Any] = Field(default_factory=dict)
    # DPDP purpose-binding — required to register (validated in the handler).
    lawful_basis: Optional[str] = None
    purpose: Optional[str] = Field(default=None, max_length=2000)
    notice_posted: bool = False
    # STQC/BIS-ER posture. Optional at create — unlike lawful_basis this never
    # blocks registration; omitting it leaves the camera honestly "unknown".
    stqc_status: Optional[str] = None
    stqc_certificate_no: Optional[str] = Field(default=None, max_length=128)
    stqc_valid_until: Optional[date] = None

    @field_validator("rtsp_url")
    @classmethod
    def _rtsp_scheme(cls, v: str) -> str:
        if not v.startswith(("rtsp://", "rtsps://")):
            raise ValueError("rtsp_url must start with rtsp:// or rtsps://")
        return v

    @field_validator("slug", mode="before")
    @classmethod
    def _slug_fmt(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        return validate_slug_format(v)


class CameraUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    rtsp_url: Optional[str] = None
    sensor_id: Optional[uuid.UUID] = None
    enabled: Optional[bool] = None
    recording: Optional[bool] = None
    motion_detection: Optional[bool] = None
    motion_sensitivity: Optional[Literal["low", "medium", "high"]] = None
    search_indexing: Optional[bool] = None
    # "face" is accepted but is NOT in the default set below: measured on this
    # appliance, a usable face appears on ~4% of person passes, so the domain is
    # something an operator turns on for a close-range camera, not something
    # every camera inherits. See services/analytics/analytics/faces.py.
    search_domains: Optional[list[Literal["person", "vehicles", "plate", "face"]]] = None
    # Days the NVR keeps this camera's footage. 0 resets to the appliance
    # default (NVR_DEFAULT_RETENTION_DAYS); omitted/None leaves it unchanged.
    retention_days: Optional[int] = Field(default=None, ge=0, le=3650)
    # Why this retention period is set (DPDP purpose-binding). Recorded on the
    # camera and echoed into the retention.changed audit entry.
    retention_justification: Optional[str] = Field(default=None, max_length=1000)
    # DPDP lawful basis / purpose / notice — editable after onboarding.
    lawful_basis: Optional[str] = None
    purpose: Optional[str] = Field(default=None, max_length=2000)
    notice_posted: Optional[bool] = None
    # STQC/BIS-ER posture — editable after onboarding, which is the common path:
    # certification is usually attested during an audit, long after registration.
    # Setting stqc_status stamps stqc_verified_at/_by from the acting principal.
    stqc_status: Optional[str] = None
    stqc_certificate_no: Optional[str] = Field(default=None, max_length=128)
    stqc_valid_until: Optional[date] = None
    # Days before this camera's footage is groomed to keyframe-only. Same
    # sentinel scheme as retention_days: 0 resets to the appliance default,
    # omitted/None leaves it unchanged. >= retention means never groomed.
    groom_after_days: Optional[int] = Field(default=None, ge=0, le=3650)
    metadata: Optional[dict[str, Any]] = None

    @field_validator("rtsp_url", mode="before")
    @classmethod
    def _rtsp_scheme(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.startswith(("rtsp://", "rtsps://")):
            raise ValueError("rtsp_url must start with rtsp:// or rtsps://")
        return v


def _sub_track_out(camera: Camera) -> Optional[dict[str, Any]]:
    """`sub_track` plus the retention that will actually be applied.

    Imported lazily: services.tracks imports from this module, so a top-level
    import here would be circular.
    """
    if not camera.sub_track:
        return None
    from .services import tracks  # noqa: PLC0415 — see docstring

    return {
        **camera.sub_track,
        "effective_retention_days": tracks.sub_retention_days(
            camera, camera.retention_days),
    }


class CameraResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    rtsp_url: str
    local_rtsp_url: str
    sensor_id: Optional[uuid.UUID]
    created_at: datetime
    updated_at: datetime
    enabled: bool
    recording: bool
    last_seen_at: Optional[datetime]
    health_status: str
    metadata: dict[str, Any]
    # When the relay stream last became ready (MediaMTX readyTime) — the true
    # "up since" for uptime display. Second precision, ISO-8601 Z. None when
    # the stream is down or MediaMTX was unreachable during enrichment.
    ready_since: Optional[str] = None
    privacy_masks: list[Any] = Field(default_factory=list)
    # Recording calendar; None = record 24/7 (see services/schedule.py).
    recording_schedule: Optional[dict[str, Any]] = None
    # Motion detection opt-in + sensitivity preset (None = service default).
    motion_detection: bool = False
    motion_sensitivity: Optional[str] = None
    # Whether Smart Search indexes this camera. Carried on every camera the SPA
    # loads so the search page can tell "found nothing" from "never looked".
    search_indexing: bool = True
    search_domains: list[str] = Field(default_factory=lambda: ["person", "vehicles"])
    # NVR retention override in days; None = appliance default. The default
    # itself rides along so the UI can show the effective policy and warn
    # when a change would shorten it (footage deletion).
    retention_days: Optional[int] = None
    retention_default_days: int = 30
    # Why this retention is set (DPDP purpose-binding); None when never recorded.
    retention_justification: Optional[str] = None
    # DPDP lawful basis + purpose + notice/signage flag (reportable posture).
    lawful_basis: Optional[str] = None
    purpose: Optional[str] = None
    notice_posted: bool = False
    # STQC/BIS-ER posture + attestation provenance (reportable; see migration 023).
    stqc_status: str = "unknown"
    stqc_certificate_no: Optional[str] = None
    stqc_valid_until: Optional[date] = None
    stqc_verified_at: Optional[datetime] = None
    stqc_verified_by: Optional[str] = None
    # Groom-after override in days; None = the NVR's appliance default
    # (which the UI reads from the NVR /storage endpoint).
    groom_after_days: Optional[int] = None
    # CMM analytics config (activity→zone mapping + polygon zones). {} = unset.
    analytics_config: dict[str, Any] = Field(default_factory=dict)
    # Inventory columns (from discovery; None for manually-added cameras).
    ip: Optional[str] = None
    vendor: Optional[str] = None
    model: Optional[str] = None
    firmware: Optional[str] = None
    # True when stream settings (bitrate/fps/resolution) can be managed over
    # ONVIF: we know the device's ONVIF port and hold encrypted credentials.
    onvif_capable: bool = False
    # For the credentials form prefill; None when never probed.
    onvif_port: Optional[int] = None
    # The camera's low-resolution second stream, or null when it has none or
    # none has been resolved yet. Enriched on the way out with
    # `effective_retention_days` — what the sub is ACTUALLY kept for once the
    # per-camera value, the appliance default and the clamp to the main have all
    # been applied. Without it a camera on the default shows a daily cost and no
    # total, because the browser cannot know the default or the clamp.
    sub_track: Optional[dict[str, Any]] = None

    @classmethod
    def from_orm(cls, camera: Camera, local_rtsp_url: str) -> "CameraResponse":
        from .config import settings  # local import — models must stay import-light

        return cls(
            retention_default_days=settings.nvr_default_retention_days,
            onvif_capable=bool(camera.ip and camera.onvif_port and camera.enc_password),
            onvif_port=camera.onvif_port,
            privacy_masks=camera.privacy_masks or [],
            sub_track=_sub_track_out(camera),
            recording_schedule=camera.recording_schedule,
            motion_detection=camera.motion_detection,
            search_indexing=camera.search_indexing,
            search_domains=list(camera.search_domains or []),
            motion_sensitivity=camera.motion_sensitivity,
            retention_days=camera.retention_days,
            retention_justification=camera.retention_justification,
            lawful_basis=camera.lawful_basis,
            purpose=camera.purpose,
            notice_posted=camera.notice_posted,
            stqc_status=camera.stqc_status or "unknown",
            stqc_certificate_no=camera.stqc_certificate_no,
            stqc_valid_until=camera.stqc_valid_until,
            stqc_verified_at=camera.stqc_verified_at,
            stqc_verified_by=camera.stqc_verified_by,
            groom_after_days=camera.groom_after_days,
            analytics_config=camera.analytics_config or {},
            ip=camera.ip,
            vendor=camera.vendor,
            model=camera.model,
            firmware=camera.firmware,
            id=camera.id,
            name=camera.name,
            slug=camera.slug,
            rtsp_url=camera.rtsp_url,
            local_rtsp_url=local_rtsp_url,
            sensor_id=camera.sensor_id,
            created_at=camera.created_at,
            updated_at=camera.updated_at,
            enabled=camera.enabled,
            recording=camera.recording,
            last_seen_at=camera.last_seen_at,
            health_status=camera.health_status,
            metadata=camera.camera_metadata,
        )


class UptimeInterval(BaseModel):
    status: str          # connected | disconnected | error | unknown | disabled
    start: datetime
    end: datetime


class CameraUptimeResponse(BaseModel):
    """Reconstructed status timeline for one camera over a time window.

    uptime_pct is computed over MONITORED time only (connected vs
    disconnected/error); 'unknown' and 'disabled' spans are excluded from the
    denominator and reported separately, so a service restart or an
    intentionally disabled camera never counts as an outage.
    """
    model_config = ConfigDict(populate_by_name=True)

    camera_id: uuid.UUID
    slug: str
    from_: datetime = Field(alias="from")
    to: datetime
    uptime_pct: Optional[float]   # None when the window has no monitored time
    outages: int
    up_seconds: float
    down_seconds: float
    unknown_seconds: float
    intervals: list[UptimeInterval]


class CameraHealthResponse(BaseModel):
    slug: str
    connected: bool
    status: str
    checked_at: Optional[str]
    reconnect_count: int
    tracks: list[Any]   # MediaMTX may return strings or dicts depending on version
    source_type: Optional[str]


# ── Bulk upload ───────────────────────────────────────────────────────────────

class BulkRowResult(BaseModel):
    row: int
    name: Optional[str] = None
    success: bool
    slug: Optional[str] = None
    error: Optional[str] = None


class BulkUploadResponse(BaseModel):
    total: int
    success_count: int
    error_count: int
    dry_run: bool
    results: list[BulkRowResult]


# ── Discovery (device-centric view of pre-registration camera rows) ──────────
# Wire format is kept byte-compatible with the old discovery microservice so
# the SPA needs no changes: `status` uses the legacy value "added" where the
# unified lifecycle says stage="registered".

class DeviceOut(BaseModel):
    id: uuid.UUID
    ip: Optional[str]
    mac: Optional[str]
    onvif_port: Optional[int]
    rtsp_port: Optional[int]
    vendor: Optional[str]
    model: Optional[str]
    firmware: Optional[str]
    status: str
    rtsp_candidates: list[Any]
    # The camera's low-resolution second stream, or null when it has none.
    sub_track: Optional[dict[str, Any]] = None
    open_ports: list[Any]
    has_credentials: bool
    username: Optional[str]
    error: Optional[str]
    last_scanned_at: Optional[datetime]

    @classmethod
    def from_camera(cls, c: "Camera", username: Optional[str]) -> "DeviceOut":
        status = "added" if c.stage == CameraStage.REGISTERED.value else c.stage
        return cls(
            id=c.id,
            ip=c.ip,
            mac=c.mac,
            onvif_port=c.onvif_port,
            rtsp_port=c.rtsp_port,
            vendor=c.vendor,
            model=c.model,
            firmware=c.firmware,
            status=status,
            rtsp_candidates=c.rtsp_candidates or [],
            sub_track=c.sub_track,
            open_ports=c.open_ports or [],
            has_credentials=bool(c.enc_password),
            username=username,
            error=c.discovery_error,
            last_scanned_at=c.last_scanned_at,
        )


class ScanBody(BaseModel):
    cidr: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None


class CredentialBody(BaseModel):
    username: str
    password: str


class AddBody(BaseModel):
    profile_index: int = 0
    name: Optional[str] = None
    enabled: bool = True
    recording: bool = Field(default=True, description="Start NVR recording on add")
    zone: Optional[str] = Field(
        default=None, max_length=64,
        description="Zone / group label stored in camera metadata (onboarding wizard)",
    )
    # Free-form onboarding metadata merged into camera_metadata (wizard Assign
    # step: gps, installed_at, …). Never interpreted here. Lawful basis moved out
    # of here into first-class fields below.
    metadata: Optional[dict[str, Any]] = None
    # DPDP purpose-binding — required to register (validated in _register).
    lawful_basis: Optional[str] = None
    purpose: Optional[str] = None
    notice_posted: bool = False
    # Re-probe the chosen profile before promoting. On by default: the scan's
    # result can be stale, and a stream that won't play is registered disabled
    # rather than left flapping in the health monitor. Set false to force-add a
    # camera the probe can't reach (firewalled probe host, transient outage).
    verify: bool = True


class EncoderUpdateBody(BaseModel):
    """Partial update of one video encoder configuration — omitted fields are
    left untouched on the camera. width/height must be set together."""

    width: Optional[int] = Field(default=None, ge=1)
    height: Optional[int] = Field(default=None, ge=1)
    fps: Optional[int] = Field(default=None, ge=1, le=240)
    bitrate_kbps: Optional[int] = Field(default=None, ge=1)
    gov_length: Optional[int] = Field(default=None, ge=1, description="GOP / I-frame interval")
    quality: Optional[float] = Field(default=None, ge=0)
    h264_profile: Optional[str] = Field(default=None, description="e.g. Baseline/Main/High")

    @model_validator(mode="after")
    def _pair(self) -> "EncoderUpdateBody":
        if (self.width is None) != (self.height is None):
            raise ValueError("width and height must be provided together")
        return self


class ImagingUpdateBody(BaseModel):
    """Partial update of one video source's imaging settings — omitted fields
    are left untouched on the camera. Mode strings (ir_cut_filter, wdr_mode,
    exposure_mode, …) must come from the camera-reported options."""

    brightness: Optional[float] = None
    contrast: Optional[float] = None
    color_saturation: Optional[float] = None
    sharpness: Optional[float] = None
    ir_cut_filter: Optional[str] = Field(default=None, description="ON / OFF / AUTO")
    wdr_mode: Optional[str] = None
    wdr_level: Optional[float] = None
    blc_mode: Optional[str] = None
    blc_level: Optional[float] = None
    exposure_mode: Optional[str] = None
    exposure_time: Optional[float] = None
    gain: Optional[float] = None
    iris: Optional[float] = None
    wb_mode: Optional[str] = None
    wb_cr_gain: Optional[float] = None
    wb_cb_gain: Optional[float] = None
    focus_mode: Optional[str] = None


class SubTrackBody(BaseModel):
    """Switch a resolved sub track's recording on or off, and size its retention."""

    recording_enabled: bool
    # Days to keep the sub's footage. None leaves it as-is (appliance default on
    # first enable). Clamped to the main's retention — a scrub track outliving
    # the footage it helps scrub is pure waste.
    retention_days: Optional[int] = Field(default=None, ge=1, le=3650)


class MasksBody(BaseModel):
    """Privacy mask polygons in normalized image coordinates (0–1)."""

    masks: list[list[list[float]]] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def _valid_polygons(self) -> "MasksBody":
        for poly in self.masks:
            if not 3 <= len(poly) <= 64:
                raise ValueError("Each mask polygon needs 3–64 points")
            for pt in poly:
                if len(pt) != 2 or not all(0.0 <= v <= 1.0 for v in pt):
                    raise ValueError("Mask points must be [x, y] with 0 <= x,y <= 1")
        return self


# CMM analytics — the default activity catalog is seeded into the
# analytics_activity_types table by migration 014. After that the catalog is
# admin-managed in the DB; activity `type` is validated against it at the route.
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_TYPE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_PARAM_KEY = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")


class ParamField(BaseModel):
    """One DETECTOR parameter a catalog type exposes to the AI Config UI —
    never the schedule (active_hours/active_days), which stays built-in on
    every activity regardless of type."""

    key: str
    label: str = Field(min_length=1, max_length=80)
    kind: Literal["number", "text", "bool", "list", "json", "enum"]
    default: Any = None
    min: Optional[float] = None
    max: Optional[float] = None
    unit: Optional[str] = None   # e.g. "s", "frames", "%"
    help: Optional[str] = None
    # From the activity's code definition: the choices of an enum, the items a
    # list may hold; a number that must be whole; how few items a list may keep.
    options: Optional[list[Any]] = None
    integer: bool = False
    min_items: Optional[int] = None
    configurable: bool = True

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        if not _PARAM_KEY.match(v):
            raise ValueError("key must be [a-z0-9_], 1–64 chars, starting with alnum")
        return v


class ActivityTypeItem(BaseModel):
    """One entry in the activity-type catalog as the admin page sends it. Only
    `label` and `color` (and the list order) are applied — the key must already
    be in the catalog and `params_schema` is ignored: both come from the CPU
    activity registry."""

    key: str
    label: str = Field(min_length=1, max_length=120)
    color: str
    # Per-type detector parameter schema. THREE-VALUED on purpose:
    #   None -> omitted by the client; replace_activity_types carries the
    #           type's currently-stored schema forward unchanged (so an old
    #           client, or a UI that only edits label/color, can't silently
    #           wipe it).
    #   []   -> explicitly "this type has no extra settings" (or "clear the
    #           schema"), stored as an empty list.
    #   [...] -> the new schema, used as given.
    # list_activity_types always returns a concrete list, never None — this
    # tri-state only matters on the way in.
    params_schema: Optional[list[ParamField]] = Field(default=None, max_length=32)

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        if not _TYPE_KEY.match(v):
            raise ValueError("key must be [A-Za-z0-9_-], 1–64 chars, not starting with _ or -")
        return v

    @field_validator("color")
    @classmethod
    def _color(cls, v: str) -> str:
        if not _HEX_COLOR.match(v):
            raise ValueError("color must be a #RRGGBB hex string")
        return v


class ActivityTypesBody(BaseModel):
    """Replace-all payload for the activity-type catalog (admin PUT)."""

    types: list[ActivityTypeItem] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def _unique(self) -> "ActivityTypesBody":
        keys = [t.key for t in self.types]
        if len(keys) != len(set(keys)):
            raise ValueError("Activity type keys must be unique")
        return self


class AnalyticsRegion(BaseModel):
    """A named region in normalized image coordinates (0–1): either a polygon
    `zone` (3–64 points) or a `tripwire` line (exactly 2 points, with a crossing
    direction). Activities reference one or more regions."""

    kind: Literal["zone", "tripwire"]
    name: str = Field(min_length=1, max_length=64)
    color: str
    points: list[list[float]]
    direction: Optional[Literal["both", "a2b", "b2a"]] = None   # tripwire only

    @field_validator("color")
    @classmethod
    def _color(cls, v: str) -> str:
        if not _HEX_COLOR.match(v):
            raise ValueError("color must be a #RRGGBB hex string")
        return v

    @model_validator(mode="after")
    def _geom(self) -> "AnalyticsRegion":
        for pt in self.points:
            if len(pt) != 2 or not all(0.0 <= c <= 1.0 for c in pt):
                raise ValueError("Region points must be [x, y] with 0 <= x,y <= 1")
        if self.kind == "zone":
            if not 3 <= len(self.points) <= 64:
                raise ValueError("A zone needs 3–64 points")
        else:  # tripwire
            if len(self.points) != 2:
                raise ValueError("A tripwire needs exactly 2 points")
        return self


class AnalyticsActivity(BaseModel):
    """One activity: a catalog `type` the pipeline detects, owning the set of
    `regions` (zones/tripwires) it watches, plus its parameter block. This
    matches the DeepStream pipeline's semantics — activities are keyed by
    type, each type owning N zones and one parameter block — rather than the
    VMS's earlier per-instance shape. `type` is unique across a camera's
    activities; there is no per-instance `id`/`name` any more (the catalog
    label is the display name)."""

    type: str = Field(min_length=1, max_length=64)
    # May be empty: whether an activity needs a zone is its definition's zone
    # rule, checked at the route (services/activity_catalog.validate_activities).
    regions: list[str] = Field(default_factory=list, max_length=32)
    params: dict[str, Any] = Field(default_factory=dict)


class AnalyticsConfigBody(BaseModel):
    """CMM config: named regions (zones + tripwires) + activities grouped by
    detection type, each owning the regions it watches. The stored shape and
    the interface the DeepStream pipeline pulls (see the design spec)."""

    regions: dict[str, AnalyticsRegion] = Field(default_factory=dict)
    activities: list[AnalyticsActivity] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def _valid(self) -> "AnalyticsConfigBody":
        if len(self.regions) > 32:
            raise ValueError("At most 32 regions per camera")
        seen_types: set[str] = set()
        for act in self.activities:
            if act.type in seen_types:
                raise ValueError(f"Activity type '{act.type}' appears more than once")
            seen_types.add(act.type)
            seen_regions: set[str] = set()
            for rid in act.regions:
                if rid in seen_regions:
                    raise ValueError(f"Activity '{act.type}' references region '{rid}' more than once")
                seen_regions.add(rid)
                if rid not in self.regions:
                    raise ValueError(f"Activity '{act.type}' references unknown region '{rid}'")
        return self


_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$|^24:00$")


class ScheduleRule(BaseModel):
    """One recording window: a set of days plus a start–end time range.

    end <= start wraps past midnight into the following day; "24:00" means
    end-of-day. Day numbers are ISO weekdays (0=Mon) in weekly mode, days of
    the month (1–31) in monthly mode — range-checked by RecordingSchedule.
    """

    days: list[int] = Field(min_length=1, max_length=31)
    start: str = "00:00"
    end: str = "24:00"

    @field_validator("start", "end")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError("Times must be HH:MM (24h; '24:00' allowed as an end)")
        return v

    @model_validator(mode="after")
    def _sane_range(self) -> "ScheduleRule":
        if self.start == "24:00":
            raise ValueError("start cannot be 24:00")
        if self.start == self.end:
            raise ValueError("start and end must differ (use 00:00–24:00 for a full day)")
        if len(set(self.days)) != len(self.days):
            raise ValueError("Duplicate day in rule")
        return self


class RecordingSchedule(BaseModel):
    mode: Literal["weekly", "monthly"]
    rules: list[ScheduleRule] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _days_in_range(self) -> "RecordingSchedule":
        lo, hi = (0, 6) if self.mode == "weekly" else (1, 31)
        for rule in self.rules:
            for d in rule.days:
                if not lo <= d <= hi:
                    raise ValueError(
                        f"Day {d} out of range for {self.mode} mode ({lo}–{hi})"
                    )
        return self


class RecordingScheduleBody(BaseModel):
    """PUT /cameras/{id}/schedule payload. schedule=null clears it (record 24/7)."""

    schedule: Optional[RecordingSchedule] = None


class OnvifCredentialsBody(BaseModel):
    """(Re-)enter a registered camera's ONVIF credentials. Verified against the
    device before being stored (Fernet-encrypted) on the camera row."""

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(max_length=128)
    onvif_port: Optional[int] = Field(
        default=None, ge=1, le=65535,
        description="Override / set the ONVIF port (defaults to the stored one)",
    )


class OsdUpdateBody(BaseModel):
    """Partial update of one OSD overlay."""

    text: Optional[str] = Field(default=None, max_length=255)
    position: Optional[str] = Field(
        default=None, description="UpperLeft / UpperRight / LowerLeft / LowerRight / Custom"
    )
    date_format: Optional[str] = None
    time_format: Optional[str] = None


class ManualAddBody(BaseModel):
    """Add a non-ONVIF / undiscovered camera by raw RTSP URL.

    Credentials may be inline in `rtsp_url` or supplied via `username`/`password`;
    the explicit fields win when both are present.
    """
    rtsp_url: str
    name: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    enabled: bool = True
    recording: bool = Field(default=True, description="Start NVR recording on add")
    zone: Optional[str] = Field(default=None, max_length=64)
    metadata: Optional[dict[str, Any]] = None
    # DPDP purpose-binding — required to register (validated in _register).
    lawful_basis: Optional[str] = None
    purpose: Optional[str] = None
    notice_posted: bool = False
    verify: bool = True

    @field_validator("rtsp_url")
    @classmethod
    def _valid_rtsp(cls, v: str) -> str:
        v = v.strip()
        if not v.lower().startswith(("rtsp://", "rtsps://")):
            raise ValueError("rtsp_url must start with rtsp:// or rtsps://")
        return v


# ── System health ─────────────────────────────────────────────────────────────

class SystemHealthResponse(BaseModel):
    status: str
    total_cameras: int
    enabled_cameras: int
    connected_streams: int
    disconnected_streams: int
    unknown_streams: int
    mediamtx_reachable: bool
    postgres_reachable: bool
    # Appliance host metrics (System health node card). Load-average based CPU
    # (no sampling window needed) + /proc/meminfo RAM; None where unavailable.
    host: Optional[dict[str, Any]] = None
    redis_reachable: bool



# ── Sitemaps (Map tab) ────────────────────────────────────────────────────────

SITEMAP_MAX_BYTES: int = 10 * 1024 * 1024

# Magic-byte sniffing is the upload security boundary — filename and client
# Content-Type are attacker-controlled. SVG is text, so acceptance is
# whitelist-shaped: after skipping whitespace, an optional <?xml ?> declaration,
# comments, and a <!DOCTYPE svg ...> declaration, the FIRST real tag must be
# <svg. Anything else (<!doctype html>, <html>, comments hiding html) is None.
def _first_real_tag_is_svg(head: bytes) -> bool:
    i, n = 0, len(head)
    while i < n:
        while i < n and head[i:i + 1].isspace():
            i += 1
        if i >= n or head[i:i + 1] != b"<":
            return False
        rest = head[i:]
        low = rest.lower()
        if low.startswith(b"<?xml"):
            end = rest.find(b"?>")
            if end == -1:
                return False
            i += end + 2
            continue
        if low.startswith(b"<!--"):
            end = rest.find(b"-->", 4)
            if end == -1:
                return False
            i += end + 3
            continue
        if low.startswith(b"<!doctype"):
            end = rest.find(b">")
            if end == -1 or not low[len(b"<!doctype"):end].strip().startswith(b"svg"):
                return False
            i += end + 1
            continue
        # The first real tag: accept only <svg with a proper name boundary.
        return low.startswith(b"<svg") and low[4:5] in (b"", b">", b"/", b" ", b"\t", b"\r", b"\n")
    return False


# ── Sitemap dimensions ───────────────────────────────────────────────────────
# Bounds, and why each one exists:
#   MIN_EDGE  a plan smaller than this can't be read at 1:1, let alone at the
#             map's 6x zoom, and pin placement gets coarse — on a 200px plan
#             one pixel is half a percent of the floor.
#   MAX_EDGE  the byte cap does NOT bound decode cost: a flat-colour PNG
#   MAX_PIXELS  compresses enormously, so a 12000x12000 image fits inside 10 MB
#             and decodes to ~576 MB of RGBA in every operator's browser. The
#             appliance stores it happily; the clients are what fall over.
#   ASPECT    beyond this the surface (which honours the image's aspect exactly)
#             renders a thin strip or a narrow column.
SITEMAP_MIN_EDGE_PX: int = 600
SITEMAP_MAX_EDGE_PX: int = 12_000
SITEMAP_MAX_PIXELS: int = 40_000_000
SITEMAP_MAX_ASPECT: float = 8.0

_JPEG_SOF_MARKERS = frozenset({
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
})


def _png_dimensions(data: bytes) -> Optional[tuple[int, int]]:
    # IHDR is mandatory and always the first chunk: 8-byte signature, 4-byte
    # length, "IHDR", then width and height as big-endian uint32.
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    return (
        int.from_bytes(data[16:20], "big"),
        int.from_bytes(data[20:24], "big"),
    )


def _jpeg_dimensions(data: bytes) -> Optional[tuple[int, int]]:
    # Walk the segment chain to the first SOF (start-of-frame), which carries
    # the size. Height precedes width, both big-endian uint16.
    i, n = 2, len(data)
    while i + 9 <= n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        # Standalone markers carry no length field.
        if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        if seg_len < 2:
            return None
        if marker in _JPEG_SOF_MARKERS:
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return (w, h) if w and h else None
        i += 2 + seg_len
    return None


def _webp_dimensions(data: bytes) -> Optional[tuple[int, int]]:
    # Three sub-formats, three headers. Sizes are stored minus one in the two
    # newer ones, which is the classic off-by-one in hand-rolled parsers.
    if len(data) < 30:
        return None
    fourcc = data[12:16]
    if fourcc == b"VP8X":                       # extended (canvas size)
        return (
            int.from_bytes(data[24:27], "little") + 1,
            int.from_bytes(data[27:30], "little") + 1,
        )
    if fourcc == b"VP8L":                       # lossless
        if data[20] != 0x2F:                    # signature byte
            return None
        bits = int.from_bytes(data[21:25], "little")
        return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if fourcc == b"VP8 ":                       # lossy
        if data[23:26] != b"\x9d\x01\x2a":     # keyframe sync code
            return None
        return (
            int.from_bytes(data[26:28], "little") & 0x3FFF,
            int.from_bytes(data[28:30], "little") & 0x3FFF,
        )
    return None


def image_dimensions(data: bytes, content_type: str) -> Optional[tuple[int, int]]:
    """(width, height) read from the file header, or None if not derivable.

    Header parsing rather than a decode: it never expands attacker-controlled
    bytes in memory (the whole point of the size guard below), and it keeps the
    API free of an image library. SVG returns None by design — it has no pixel
    dimensions to bound, and the browser resolves its size from width/height or
    viewBox at render time."""
    try:
        if content_type == "image/png":
            return _png_dimensions(data)
        if content_type == "image/jpeg":
            return _jpeg_dimensions(data)
        if content_type == "image/webp":
            return _webp_dimensions(data)
    except (IndexError, ValueError):            # truncated or malformed header
        return None
    return None


def validate_sitemap_dimensions(width: int, height: int) -> Optional[str]:
    """None if the plan is usable, else the reason it isn't (shown to the user)."""
    if width <= 0 or height <= 0:
        return "Could not read the image dimensions"
    longest, shortest = max(width, height), min(width, height)
    if longest < SITEMAP_MIN_EDGE_PX:
        return (
            f"Image is too small ({width}x{height}). A site plan needs at least "
            f"{SITEMAP_MIN_EDGE_PX}px on its longest side to stay readable when "
            f"zoomed in."
        )
    if longest > SITEMAP_MAX_EDGE_PX:
        return (
            f"Image is too large ({width}x{height}). Keep each side under "
            f"{SITEMAP_MAX_EDGE_PX}px — bigger plans exhaust memory in the browser."
        )
    if width * height > SITEMAP_MAX_PIXELS:
        return (
            f"Image has too many pixels ({width * height / 1_000_000:.0f} MP). "
            f"Keep it under {SITEMAP_MAX_PIXELS // 1_000_000} MP — bigger plans "
            f"exhaust memory in the browser."
        )
    if longest / shortest > SITEMAP_MAX_ASPECT:
        return (
            f"Image is too elongated ({width}x{height}). A plan wider or taller "
            f"than {SITEMAP_MAX_ASPECT:.0f}:1 renders as a thin strip — crop it "
            f"or split it across two plans."
        )
    return None


def sniff_image_content_type(data: bytes) -> Optional[str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if _first_real_tag_is_svg(data[:512]):
        return "image/svg+xml"
    return None


class CalibrationPoint(BaseModel):
    """One georeferencing control point: a normalized image position (x/y in
    0–1) tied to a real-world coordinate."""

    model_config = ConfigDict(extra="forbid")

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    lat: float = Field(ge=-90.0, le=90.0)
    lng: float = Field(ge=-180.0, le=180.0)


class CalibrationBody(BaseModel):
    """Set (2–3 points) or clear (empty list) a sitemap's georeferencing."""

    points: list[CalibrationPoint] = Field(default_factory=list)

    @model_validator(mode="after")
    def _count(self) -> "CalibrationBody":
        n = len(self.points)
        if n not in (0, 2, 3):
            raise ValueError(
                "calibration needs exactly 2 or 3 control points (or 0 to clear)"
            )
        return self


PERIPHERAL_CATEGORIES: tuple[str, ...] = ("Lighting", "Access control", "Audio · Sensors")


class PeripheralIn(BaseModel):
    """Operator-supplied peripheral facts. Note what is absent: no state and no
    `last_seen`. Those are a bridge's to report — accepting them here would let
    the wall show a hand-typed "LOCKED" that nothing verified."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    category: Literal["Lighting", "Access control", "Audio · Sensors"]
    location: Optional[str] = Field(default=None, max_length=160)
    vendor: Optional[str] = Field(default=None, max_length=120)
    # Where a bridge will find this device (HA entity id, MQTT topic, relay
    # address). Free-form until an integration fixes the format.
    external_id: Optional[str] = Field(default=None, max_length=200)
    notes: Optional[str] = Field(default=None, max_length=2000)
    enabled: bool = True

    @field_validator("name", "location", "vendor", "external_id", "notes")
    @classmethod
    def _strip(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None


class PeripheralPatch(BaseModel):
    """Partial update — every field optional, same exclusions as PeripheralIn."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    category: Optional[Literal["Lighting", "Access control", "Audio · Sensors"]] = None
    location: Optional[str] = Field(default=None, max_length=160)
    vendor: Optional[str] = Field(default=None, max_length=120)
    external_id: Optional[str] = Field(default=None, max_length=200)
    notes: Optional[str] = Field(default=None, max_length=2000)
    enabled: Optional[bool] = None

    @field_validator("name", "location", "vendor", "external_id", "notes")
    @classmethod
    def _strip(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None


class PeripheralOut(BaseModel):
    """A peripheral as the SPA sees it. `last_state`/`last_seen` are null until
    a bridge writes them — the UI renders that as "state unknown", never as a
    healthy device."""

    id: str          # slug: the stable key the map's placements reference
    name: str
    category: str
    location: Optional[str] = None
    vendor: Optional[str] = None
    external_id: Optional[str] = None
    notes: Optional[str] = None
    enabled: bool
    last_state: Optional[str] = None
    last_seen: Optional[datetime] = None
    # How many plans this device is pinned to — the delete confirmation needs
    # it, and the inventory wall shows "not on any plan" without a second call.
    placements: int = 0


class HaPlacement(BaseModel):
    """One HA peripheral pinned to a plan: its peripherals id and a normalized
    image position, the same 0–1 convention camera placement uses."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class HaPlacementsBody(BaseModel):
    """Replace a sitemap's HA placements wholesale — an empty list clears them.

    Whole-list rather than per-device PATCH because the Map tab always holds
    the full set in hand, and a replace can't leave the plan half-updated if
    two operators save at once: last write wins, visibly."""

    devices: list[HaPlacement] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _unique_ids(self) -> "HaPlacementsBody":
        ids = [d.id for d in self.devices]
        if len(ids) != len(set(ids)):
            raise ValueError("each device can be placed at most once per map")
        return self


class SitemapMeta(BaseModel):
    id: int
    name: str
    content_type: str
    created_at: datetime
    # Cameras with coordinates on this plan (dots you can see) vs. every
    # camera assigned to it, placed or still in the unplaced tray.
    cameras_placed: int
    cameras_assigned: int = 0
    # The stored control points, or None when uncalibrated. Present so the Map
    # page knows whether GPS auto-placement is available for this map.
    calibration: Optional[list[dict[str, float]]] = None
    # HA peripherals pinned to this plan ({id, x, y}), or None when none are.
    ha_devices: Optional[list[HaPlacement]] = None


