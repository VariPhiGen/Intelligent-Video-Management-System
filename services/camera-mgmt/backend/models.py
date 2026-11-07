from __future__ import annotations

import ipaddress
import random
import re
import string
import uuid
from datetime import date, datetime
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union, get_args

from sqlalchemy import (
    BigInteger,
    SmallInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .db import Base


class SearchPlannerSettings(Base):
    __tablename__ = 'search_planner_settings'
    __table_args__ = (CheckConstraint('id = 1', name='ck_search_planner_singleton'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    credentials: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    assistant_tested_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tested_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


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
    # Most cameras answer plain-HTTP ONVIF on this port and this column stays
    # False/NULL forever. A minority of firmwares reject at least one ONVIF
    # operation over HTTP with a SOAP TransModeError ("client may not support
    # HTTPS mode") — confirmed live on a device whose GetCapabilities worked
    # over both schemes but GetSystemDateAndTime (fired by every _camera()
    # build via adjust_time) only answered over HTTPS, making PTZ/encoder/
    # imaging/OSD 502 while the RTSP stream (a separate, scheme-less path)
    # played fine. onvif-zeep decides the scheme purely from whether the host
    # string it's given already starts with "https://" (see get_definition in
    # onvif/client.py) — the port number alone never selects it. See onvif_host().
    onvif_use_https: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
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
    # What this camera's ONVIF PTZ service can do, as last probed off the
    # device (services/onvif_ptz.probe). Three distinct states, and the UI says
    # something different for each:
    #
    #   NULL          never probed — we do not know yet
    #   {supported: false, reason: "..."}   a fixed camera, asked and answered
    #   {supported: true, profile_token, continuous/relative/absolute, presets,
    #    home, aux_commands, node_token}    movable, with the move kinds it
    #                                       actually advertises
    #
    # Cached here rather than probed per request because the camera list, the
    # live grid and the config tabs all need the yes/no answer, and a SOAP
    # round trip per camera per render is not a thing we are going to do. It is
    # refreshed when ONVIF credentials are (re-)entered, on first PTZ use, and
    # on the explicit re-detect button.
    #
    # One column, not a `ptz_capable` boolean alongside it: two columns for one
    # fact drift, and `supported` already is the boolean.
    ptz_capabilities: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # Click-to-centre calibration (services/ptz_calibrate): how much RELATIVE pan/tilt moves the picture by one
    # frame width/height, measured from the camera's own frames at a few zoom levels —
    #   {"version": 1, "points": [{"zoom", "pan_per_width", "tilt_per_height"}, …], "measured_at", "by"}
    # Kept apart from ptz_capabilities on purpose: a re-probe rewrites that column, and losing a measurement
    # that physically moved the camera to take would be silly. NULL = not calibrated (fallback gain).
    ptz_calibration: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
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
    # Smart Search indexing opt-IN, like motion_detection and unlike the
    # opt-OUT this shipped as (migration 030). A newly added camera runs NO AI:
    # registering a camera is a recording decision, and person/vehicle indexing
    # is a second decision with its own cost in CPU, disk and — since the crops
    # are personal data — its own justification. Defaulting it on made both at
    # once, silently, for every camera an operator onboarded.
    #
    # The honesty problem that made it an opt-out is unchanged and still handled
    # in the SPA: "no results" is ambiguous, so Smart Search names the cameras it
    # never looked at. See migration 038 — EXISTING cameras keep whatever they
    # have; only the default for new rows moves.
    search_indexing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Which domains this camera contributes: person / vehicles / plate.
    #: "vehicles" is plural to match the wire contract and the table names.
    search_domains: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=lambda: ["person", "vehicles"]
    )
    # Per-camera NVR retention, in MINUTES of footage kept before deletion.
    # NULL = appliance default (NVR_RETENTION_MINUTES, else
    # NVR_DEFAULT_RETENTION_DAYS read as a day-spelling of the same thing).
    # Synced to the NVR on change and on every reconcile pass
    # (services/nvr_client.py). Minutes rather than days because the recorder
    # writes 60-second segments and deletes whole ones, so a minute is the
    # finest period it can actually honour — see services/retention_units.py
    # and migration 042 for why this is one column and not two.
    retention_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
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
    # Time-lapse (migration 057, services/timelapse.py). OFF by default like
    # every other per-camera capture: a newly added camera records and does
    # nothing else until somebody asks. The config is validated and defaulted by
    # services/timelapse.TimelapseConfig; {} means "all defaults".
    timelapse_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                                    server_default=text("false"))
    timelapse_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict,
                                                   server_default=text("'{}'::jsonb"))


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

    search_aliases: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    suggested_aliases: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
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



class Branding(Base):
    """Per-installation branding for the operator UI (one row, id=1): the client's logo and the
    Video Wall title. Admin-set from the wall's settings; read by every authenticated principal.
    Logo bytes live in Postgres for the same reason sitemaps do — the API container has no
    writable volume — and are served with an ETag from /api/branding/logo."""

    __tablename__ = "branding"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wall_title: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    logo: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    logo_content_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Dark-theme logo (052). `logo` above is the light-theme one; with only one uploaded, the SPA
    # uses it for both themes — a client with a single logo shouldn't have to upload it twice.
    logo_dark: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    logo_dark_content_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # The admin's DEFAULT video-wall settings (cameras, hide-offline, padding, header size, per-theme
    # colours). Every browser starts from these and may override single fields locally — see
    # frontend-react/src/pages/live/wallPrefs.ts. NULL = built-in defaults. Validated by
    # routers/branding.WallDefaults before it is stored.
    wall_defaults: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


BRANDING_LOGO_MAX_BYTES = 2 * 1024 * 1024


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


class CameraRegistration(Base):
    """One camera view calibrated against one plan — see migration 050.

    A `Sitemap` placement says where a camera *stands*; this says what ground it
    *covers*, as a homography fitted from landmark pairs. That is what lets a
    detection region be drawn once on the plan and projected into every camera
    that sees it, rather than redrawn per camera (plan Section 22.7).

    One row per (camera, plan, view). `preset_token` is `''` for the fixed view
    every camera has; a PTZ gains one row per calibrated ONVIF preset without
    any schema change. It is NOT NULL specifically so the unique constraint
    bites — Postgres treats NULLs as distinct, so a nullable column would allow
    the duplicate registration the constraint exists to forbid.
    """

    __tablename__ = "camera_registrations"
    __table_args__ = (
        UniqueConstraint("camera_id", "sitemap_id", "preset_token",
                         name="uq_camera_registration_view"),
        Index("ix_camera_registrations_sitemap_id", "sitemap_id"),
        Index("ix_camera_registrations_camera_id", "camera_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    camera_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False,
    )
    sitemap_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sitemaps.id", ondelete="CASCADE"), nullable=False,
    )
    preset_token: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("''"), default="",
    )
    # 3x3 row-major, FRAME -> PLAN. The plan -> frame direction used for zone
    # projection is derived by inverting this at use time, never stored, so the
    # two directions cannot drift apart.
    homography: Mapped[list] = mapped_column(JSONB, nullable=False)
    # The operator's clicked pairs, kept after fitting so a registration can be
    # re-fitted, audited, or drift-checked when the site changes.
    landmark_pairs: Mapped[list] = mapped_column(JSONB, nullable=False)
    # RMS in normalized plan units — metres only where the sitemap is calibrated.
    reprojection_error: Mapped[float] = mapped_column(Float, nullable=False)
    # Cached projection of the frame border onto the plan. Pure function of the
    # homography; stored because the Map tab draws every camera's coverage at
    # once, and so that changing the projection code cannot silently redraw
    # historical coverage without a visible migration.
    footprint: Mapped[list] = mapped_column(JSONB, nullable=False)
    calibrated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    calibrated_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


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
    # PTZ cameras: the preset the camera was confirmed at when this happened (migration 066) — its zones are what
    # `zone` names. NULL on fixed cameras.
    preset_id: Mapped[Optional[uuid.UUID]] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    preset_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
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


class TimelinePresence(Base):
    """One stretch of time a tracked person or vehicle was in a camera's view.

    What the playback timeline draws person/vehicle markers from (migration
    043, services/timeline.py). NOT an event: nothing decided anything, the
    tracker only saw something — which is why it is not in analytics_events.
    Segments are bounded (≤ 10 min, enforced by a CHECK) so an overlap query is
    an index range; long presences are consecutive rows merged at read time.
    """

    __tablename__ = "timeline_presence"
    __table_args__ = (
        Index("ix_presence_camera_kind_time", "camera_id", "kind", "started_at"),
        Index("ix_presence_expires_at", "expires_at"),
        CheckConstraint("kind IN ('person','vehicle','motion')", name="ck_timeline_presence_kind"),
        CheckConstraint(
            "ended_at >= started_at AND ended_at - started_at <= interval '10 minutes'",
            name="ck_timeline_presence_bounded"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    camera_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    peak_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    track_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    label: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="cpu", server_default="cpu")
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HeatmapSession(Base):
    """One continuous heat accumulation on one camera.

    A session is what the Heatmap page shows: it opens when a camera starts
    accumulating, and ends either because the AI Config schedule window closed
    (``closed``) or because an operator switched the camera off (``frozen``).
    A frozen session can be RESUMED — the analytics service still holds its
    matrix — which is why "stopped" is two states and not one.

    ``id`` is minted by the PRODUCER, like AnalyticsEvent's: analytics is the
    only thing that knows when heat actually started accumulating, and a
    client-minted id makes a retried upload idempotent instead of a second row.
    """

    __tablename__ = "heatmap_sessions"
    __table_args__ = (
        Index("ix_heatmap_sessions_camera_time", "camera_id", text("started_at DESC")),
        Index("ix_heatmap_sessions_open", "camera_id",
              postgresql_where=text("status <> 'closed'")),
        CheckConstraint("status IN ('active','frozen','closed')",
                        name="ck_heatmap_sessions_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    camera_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False
    )
    #: The activity key this session belongs to. Soft reference, as elsewhere.
    activity: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Heatmap_overlay",
        server_default="Heatmap_overlay"
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    close_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    #: The settings the heat was accumulated with, so an old artifact can still
    #: be read honestly after an operator changes them.
    params: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    points: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0,
                                        server_default=text("0"))
    frames: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0,
                                        server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PeopleDistributionSample(Base):
    """How many people were in each zone on one camera, at one instant.

    A LEVEL, NOT AN EVENT, which is why this is not an AnalyticsEvent. The
    Events page lists things that happened and that somebody may need to act on;
    "three people are in Zone 1" is true until it is not, and filing it as an
    event would bury real incidents under one row per zone per 30 seconds.

    ZONES ARE A COLUMN, not a table. They are always read together -- a sample
    is a snapshot of the whole camera -- and a zone is not an entity with a life
    of its own: it is named in the operator's activity config and can be renamed
    or removed there. As JSONB, a rename orphans nothing and "what did this
    camera look like at 14:00" is a single row.

    The pipeline's existing per-person dwell/exit records are NOT here. They go
    to Kafka as they always have, and answer the other half of the question:
    this table says how many are present now, those say how long someone stayed.
    """

    __tablename__ = "people_distribution_samples"
    __table_args__ = (
        Index("ix_pd_samples_camera_time", "camera_id", "captured_at"),
        Index("ix_pd_samples_expires", "expires_at"),
        UniqueConstraint("camera_id", "captured_at",
                         name="ux_pd_samples_camera_captured"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    camera_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"),
        nullable=False)
    #: The producer's clock: when the count was TRUE, not when it arrived.
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  nullable=False)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: [{zone_id, zone_name, current_count, dwell_seconds_max, dwell_seconds_avg}]
    zones: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    source: Mapped[str] = mapped_column(String(32), nullable=False,
                                        default="deepstream")
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)


class PeopleDistributionHour(Base):
    """Distinct people in one zone during one clock hour.

    THE SET, NOT A COUNT, and that is the whole design. The pipeline reports
    every 10 seconds, so one person standing still for 40 minutes appears in
    ~240 samples; adding counts would report 240 people. Storing the tracker ids
    and taking the length is what makes a person count ONCE -- and it is what
    makes a retried sample a no-op, because unioning the same ids changes
    nothing.

    It is also why the hour boundary needs no special case. Someone present
    06:55 -> 07:30 is reported in samples belonging to both hours, so their id
    joins both sets and they count once in each. The sample's timestamp picks
    the bucket; nothing tracks the crossing.

    `hour_start` is the floored LOCAL hour stored as UTC: an activity switched
    on at 06:30 feeds 06:00-07:00, it does not create a 06:30-07:30 bucket.
    """

    __tablename__ = "people_distribution_hours"
    __table_args__ = (
        Index("ix_pd_hours_camera_hour", "camera_id", "hour_start"),
        Index("ix_pd_hours_expires", "expires_at"),
        UniqueConstraint("camera_id", "hour_start", "zone_name",
                         name="ux_pd_hours_camera_hour_zone"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    camera_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"),
        nullable=False)
    hour_start: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 nullable=False)
    zone_name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: ["ds:cam1-m7cx:4711", ...] — its length is the hour's answer.
    tracker_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)


class HeatmapArtifact(Base):
    """One rendered overlay: the camera frame under its accumulated heat.

    BYTES LIVE IN ONE OF TWO PLACES and the row says which. An archived picture
    (``snapshot``/``final``) is written to the object store and carries
    ``object_key`` with ``image`` null; a ``live`` picture, and every row
    written before the store existed, carries ``image`` inline. The read path
    serves whichever is set, which is why nothing had to be backfilled.

    ``object_key`` was reserved for exactly this in migration 041 and is now
    used; see services/object_store.py for the key format and for why ``live``
    deliberately stays out of the store.

    Retention follows AnalyticsEvent: ``expires_at`` is stamped at insert from
    the owning camera's footage retention, so each row carries its own deadline
    and the sweep is an indexed DELETE.
    """

    __tablename__ = "heatmap_artifacts"
    __table_args__ = (
        Index("ix_heatmap_artifacts_camera_time", "camera_id", text("captured_at DESC")),
        Index("ix_heatmap_artifacts_session_time", "session_id", text("captured_at DESC")),
        Index("ix_heatmap_artifacts_expires_at", "expires_at"),
        # A retried upload carries the same session and capture time; this is
        # what makes the ingest idempotent rather than duplicating pictures.
        Index("ux_heatmap_artifacts_capture", "session_id", "kind", "captured_at",
              unique=True),
        # ONE LIVE PICTURE PER SESSION. `live` moves every ~30 s, so the
        # capture-time index above would insert a new row each time instead of
        # replacing — thousands a day that nobody reads. This partial index is
        # what makes the live upsert an upsert. Archived kinds are unaffected.
        Index("ux_heatmap_artifacts_live", "session_id", unique=True,
              postgresql_where=text("kind = 'live'")),
        CheckConstraint("kind IN ('live','snapshot','final')",
                        name="ck_heatmap_artifacts_kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("heatmap_sessions.id", ondelete="CASCADE"),
        nullable=False
    )
    camera_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False
    )
    #: "snapshot" while the session runs, "final" for the picture it ended on.
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="snapshot")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: What the picture covers: the session's start to this capture.
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    height: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="image/jpeg", server_default="image/jpeg"
    )
    image: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    #: Reserved: where the bytes live if they ever stop living in this row.
    object_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    points: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0,
                                        server_default=text("0"))
    peak: Mapped[float] = mapped_column(Float, nullable=False, default=0.0,
                                        server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PtzPreset(Base):
    """VMS's record of one preset stored on a PTZ camera (migration 063).

    The CAMERA is still what moves — GotoPreset by `token` — but VMS keeps the name, the stored position, a
    thumbnail and Home, so presets have names and pictures even on cameras whose factory presets have none,
    the rest of the system (patrols, per-preset AI zones, coverage) has a stable row to point at, and a camera
    reset that wipes its presets shows up as `missing_on_camera` instead of silently. Synced from the camera on
    every presets read (services/ptz_presets.sync)."""

    __tablename__ = "ptz_presets"
    __table_args__ = (UniqueConstraint("camera_id", "token", name="ux_ptz_presets_camera_token"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    camera_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False)
    token: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    pan: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tilt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    zoom: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_home: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    thumbnail: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    thumbnail_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    missing_on_camera: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Per-preset AI (migration 066): this view's {regions, activities} — the same shape as
    # cameras.analytics_config. Once ANY preset of a camera has activities, the camera's AI follows its presets
    # (services/ptz_views): a preset's rules run only while the camera is confirmed there.
    analytics_config: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # Full-size pictures of the view, day and night (IR) apart — what zones are drawn on and arrivals are checked
    # against; refreshed from confident matches (services/ptz_track).
    reference: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    reference_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reference_night: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    reference_night_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # The view changed under the zones (migration 067; services/ptz_view_changes): {state, reason, at, by, offset,
    # zoom_changed, previous_config}. "shifted" runs; "review" / "awaiting_picture" pause this preset's rules.
    analytics_review: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    previous_reference: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(),
                                                 onupdate=func.now())


class PtzViewArchive(Base):
    """A deleted preset's zones + rules + picture (migration 067) — kept so they can be copied onto another preset
    rather than lost with the preset. No link to ptz_presets: the preset is gone, and its token may already belong
    to a new one. Kept until "Forget"; goes with the camera."""

    __tablename__ = "ptz_view_archive"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    camera_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False)
    preset_id: Mapped[Optional[uuid.UUID]] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    token: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    pan: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tilt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    zoom: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    analytics_config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    picture: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    archived_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PtzPositionLog(Base):
    """One CHANGE of where a PTZ camera points (services/ptz_track). `state` is at_preset / off_preset / moving /
    unknown; `cause` is operator / patrol / preset / home / calibration / camera (moved with no command from
    VMS — the camera's own web UI, its own tour, a reboot) or NULL when it can't be told."""

    __tablename__ = "ptz_position_log"
    __table_args__ = (
        Index("ix_ptz_position_log_camera_at", "camera_id", "at"),
        Index("ix_ptz_position_log_at", "at"),
        Index("ix_ptz_position_log_cause_at", "cause", "at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    camera_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    preset_token: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # The preset's VMS id (ptz_presets.id; migration 065). The token is only the camera's slot number — cameras hand
    # a deleted preset's number to the next one saved — so history and everything else point at the id. No FK: the
    # history outlives a deleted preset.
    preset_id: Mapped[Optional[uuid.UUID]] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    preset_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    pan: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tilt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    zoom: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cause: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    actor: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class PtzViewHistory(Base):
    """One change of a PTZ camera's CONFIRMED view (services/ptz_track → ptz_views.on_view_change): `state` "preset"
    (arrived and the picture matched — `verified_by` picture / position) or "none" (moving, off preset, not confirmed).
    A dataset that samples only at presets reads this for its window (migration 068). Kept as long as the camera's
    position history."""

    __tablename__ = "ptz_view_history"
    __table_args__ = (
        Index("ix_ptz_view_history_camera_at", "camera_id", "at"),
        Index("ix_ptz_view_history_at", "at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    camera_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    preset_id: Mapped[Optional[uuid.UUID]] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    preset_token: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    preset_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    verified_by: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)


class TimelapseFrame(Base):
    """One time-lapse still: a privacy-masked, resized JPEG of the live relay.

    BYTES LIVE IN ONE OF TWO PLACES, exactly as HeatmapArtifact: `object_key`
    in the appliance's object store when one is configured (the normal case —
    compose ships MinIO), else `image` inline. The read path serves whichever is
    set. Rows carry their own `expires_at` (captured_at + the camera's
    time-lapse retention) so the sweep is an indexed DELETE, and the unique
    (camera_id, captured_at) index makes a retried insert a no-op.
    """

    __tablename__ = "timelapse_frames"
    __table_args__ = (
        Index("ux_timelapse_frames_capture", "camera_id", "captured_at", unique=True),
        Index("ix_timelapse_frames_camera_day", "camera_id", "local_day"),
        Index("ix_timelapse_frames_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    camera_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: The APPLIANCE-LOCAL calendar day, stored rather than derived: the page
    #: lists "days", the object key is filed by local day, and deriving it in SQL
    #: would need the appliance's zone inside every query.
    local_day: Mapped[date] = mapped_column(Date, nullable=False)
    object_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    width: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    height: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Mean luma 0-255 of the stored picture — what skip_dark decided on, kept so
    #: a render can drop dusk frames later without re-reading every JPEG.
    brightness: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class TimelapseRender(Base):
    """A time-lapse VIDEO made from a range of stills — on demand or nightly.

    A queue row as much as a result: the render loop (one at a time, leader
    only) picks the oldest `queued` row, flips it to `running`, reports
    `progress`, and ends at `done` with the bytes in the store (or the row) or
    at `failed` with the reason an operator can act on.
    """

    __tablename__ = "timelapse_renders"
    __table_args__ = (
        Index("ix_timelapse_renders_camera_time", "camera_id", text("created_at DESC")),
        Index("ix_timelapse_renders_status", "status"),
        CheckConstraint("status IN ('queued','running','done','failed')",
                        name="ck_timelapse_renders_status"),
        CheckConstraint("kind IN ('manual','daily')", name="ck_timelapse_renders_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    camera_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    range_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    range_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fps: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=24)
    overlay: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    deflicker: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    frames: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    object_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    video: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    encoder: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    requested_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TimelapseSettings(Base):
    """The appliance's time-lapse settings (one row, id=1), like `branding`.

    defaults  the site's default capture settings, stored as the keys that differ
              from TimelapseConfig's built-in defaults (NULL/{} = built-in). A
              camera's own timelapse_config holds only what IT changes on top of
              these, so changing a default here changes every camera that didn't.
    guard     disk protection thresholds (services/timelapse.GUARD_DEFAULTS).
    paused    set when the disk guard switched time-lapse off: when, why, which
              cameras (so "Turn back on" restores exactly those). NULL = not paused.
    """

    __tablename__ = "timelapse_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    defaults: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    guard: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    paused: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class PtzSettings(Base):
    """PTZ settings (one row, id=1). `history` = how long movement history is kept:
    {"operator_days": 90, "patrol_days": 14} — see services/ptz_track.retention() and migration 064."""

    __tablename__ = "ptz_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    history: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class DeepstreamSettings(Base):
    """Where the DeepStream pipeline's control API lives (one row, id=1).

    Set on Administration → DeepStream. NULL / no row = DEEPSTREAM_API_URL from
    the environment. Read by services/deepstream_client (load_url) for both the
    admin model routes and the camera sync loop — see migration 076.
    """

    __tablename__ = "deepstream_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    api_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class HeatmapChunk(Base):
    """Five minutes of heat on one camera, as a numpy array in the object store.

    AN INCREMENT, NOT A RUNNING TOTAL: ``object_key`` holds the heat ADDED
    between ``period_start`` and ``period_end`` (an .npz with one float32 array,
    ``heat``, at 1/``scale`` of the camera frame). The heatmap of any range is
    the sum of the blocks inside it, rendered once -- which is what the Heatmap
    page asks for. Blocks end on the local clock's 5-minute marks, or early when
    heat stopped being added.

    ``background_key`` is LEGACY: blocks written before migration 078 carried a
    camera frame in the object store. Nothing writes it now (the one frame per
    camera lives in ``heatmap_backgrounds``); the retention sweep still deletes
    the objects old rows name.
    """

    __tablename__ = "heatmap_chunks"
    __table_args__ = (
        Index("ux_heatmap_chunks_camera_start", "camera_id", "period_start", unique=True),
        Index("ix_heatmap_chunks_expires_at", "expires_at"),
        Index("ix_heatmap_chunks_camera_end", "camera_id", "period_end"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    camera_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    frame_width: Mapped[int] = mapped_column(Integer, nullable=False)
    frame_height: Mapped[int] = mapped_column(Integer, nullable=False)
    scale: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    points: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    peak: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    background_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Bytes of the .npz in the object store, for the per-camera storage cap.
    #: NULL on rows from before migration 079 until the sweep measures them.
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HeatmapBackground(Base):
    """The newest camera frame for one camera: what every heatmap is drawn over.

    ONE ROW PER CAMERA, overwritten every time analytics sends a frame (with
    each 5-minute heat block), so the previous frame is gone the moment the
    next arrives. Today's heatmap and any past range use the same picture.
    ``expires_at`` lets the retention sweep remove a frame nobody refreshes.
    """

    __tablename__ = "heatmap_backgrounds"

    camera_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"),
        primary_key=True
    )
    image: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HeatmapCameraState(Base):
    """The operator's runtime switch for one camera — explicitly, in the database.

    This is NOT the AI Config schedule, and changing it never rewrites one. The
    schedule says when the heatmap is ALLOWED to run; this says whether the
    operator wants it running at all. Analytics holds the same flag in memory
    only, so this row is what the reconcile loop re-asserts after a restart.
    """

    __tablename__ = "heatmap_camera_state"

    camera_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"),
        primary_key=True
    )
    runtime_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_by: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)


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


class AlertRule(Base):
    """An operator's rule: when THIS activity happens THERE, raise an alert.

    `mode` defaults to 'log_only' and that is the whole safety story for a
    feature that will eventually open gates — a rule nobody has reviewed raises
    its alert and records what it would have done, and moves nothing. Arming is
    a separate, audited authority (`rule_manage`), never a patchable field.

    `activity` is a soft reference to AnalyticsActivityType.key, not a FK, for
    the same reason AnalyticsEvent.activity is: the catalogue changes under the
    pipeline and a rule for a withdrawn type must stay readable rather than
    disappear (migration 039).
    """

    __tablename__ = "alert_rules"
    __table_args__ = (
        CheckConstraint("mode IN ('log_only','armed')", name="ck_alert_rules_mode"),
        CheckConstraint("scope_kind IN ('camera','zone','any')",
                        name="ck_alert_rules_scope_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    mode: Mapped[str] = mapped_column(
        String(12), nullable=False, default="log_only", server_default="log_only"
    )
    activity: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_value: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    # NULL = accept events the detector scored no confidence for. Distinct from
    # 0.0, which would accept everything.
    min_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    conditions: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    cooldown_s: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60, server_default="60"
    )
    max_event_age_s: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default="info", server_default="info"
    )
    actions: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # NULL = always armed, and the ONLY value that means always. A present but
    # empty or malformed schedule means not armed — see services/rules.py.
    active_hours: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class BindingIn(BaseModel):
    """What an operator supplies for a binding.

    `secret` is WRITE-ONLY — there is no matching field on BindingOut. A secret
    that round-trips through a GET is a secret in a browser's memory, a proxy
    log and a screenshot.
    """

    model_config = ConfigDict(extra="forbid")

    driver: Literal["mqtt", "homeassistant", "webhook"]
    address: str = Field(min_length=1, max_length=255)
    config: dict[str, Any] = Field(default_factory=dict)
    state_topic: Optional[str] = Field(default=None, max_length=255)
    secret: Optional[str] = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _addressing_is_coherent(self) -> "BindingIn":
        # Validated here as well as by the CHECK constraint: the constraint
        # catches the equality form of a topic loop, this catches the wildcard
        # forms, and an operator deserves a 422 naming the reason rather than a
        # 500 out of the database.
        if self.driver == "mqtt":
            from .services.bridge.mqtt import validate_topics
            reason = validate_topics(self.address, self.state_topic)
            if reason:
                raise ValueError(reason)
        elif self.state_topic:
            # HA state arrives over its own WebSocket and a webhook is outbound
            # only. A state topic on either is configuration nobody can honour,
            # and storing it would promise a wall update that never comes.
            raise ValueError("state_topic is only meaningful for the mqtt driver")
        return self


class BindingOut(BaseModel):
    """Note what is absent: `secret`. Deliberately."""

    model_config = ConfigDict(from_attributes=True)

    driver: str
    address: str
    config: dict[str, Any] = Field(default_factory=dict)
    state_topic: Optional[str] = None
    verified_at: Optional[datetime] = None
    verify_error: Optional[str] = None


class CommandIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1, max_length=40)
    params: dict[str, Any] = Field(default_factory=dict)


class PeripheralBinding(Base):
    """How the bridge reaches one peripheral.

    Separate from `Peripheral` on purpose: 029 established that the peripherals
    row holds what a PERSON can know, and this is what a MACHINE needs. Keeping
    them apart also means an operator who only curates the inventory sees an
    unchanged API, and a binding can be replaced without touching the map pins
    that FK to the peripheral.

    The primary key is the peripheral id, so one device has at most one binding.
    Two would mean two answers to "where does this live" and the second would be
    silently unused.

    `secret_enc` is Fernet ciphertext (backend/crypto.py). There is deliberately
    no plaintext column to read instead by mistake.
    """

    __tablename__ = "peripheral_bindings"
    __table_args__ = (
        CheckConstraint("driver IN ('mqtt','homeassistant','webhook')",
                        name="ck_bindings_driver"),
        CheckConstraint("state_topic IS NULL OR state_topic <> address",
                        name="ck_bindings_no_loop"),
    )

    peripheral_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("peripherals.id", ondelete="CASCADE"),
        primary_key=True)
    driver: Mapped[str] = mapped_column(String(24), nullable=False)
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    # NULL = a write-only device. A relay that reports nothing back is still
    # worth driving; it simply never contributes to last_state.
    state_topic: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    secret_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # NULL = never confirmed. An unverified binding refuses to actuate, because
    # HA entity ids move between releases and a silent no-op is worse than a
    # loud refusal.
    verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    verify_error: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 nullable=False)


class IntegrationCredential(Base):
    """Where an integration lives and how to authenticate to it (migration 081).

    One row per integration, keyed by name. `secret_enc` is Fernet ciphertext;
    as with bindings, there is deliberately no plaintext column.
    """

    __tablename__ = "integration_credentials"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    url: Mapped[str] = mapped_column(String(255), nullable=False)
    secret_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 nullable=False)
    updated_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class HaConnectionIn(BaseModel):
    """What an admin supplies. `token` is WRITE-ONLY: HaConnectionOut has no
    such field. Omitted on a save, the stored token is kept — so the URL can
    be corrected without pasting the token again."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=255)
    token: Optional[str] = Field(default=None, min_length=1, max_length=2000)

    @field_validator("url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return v


class HaConnectionOut(BaseModel):
    """What the connection panel shows. Never the token — only whether one
    is set, and where the live configuration came from."""

    #: ui — saved here; env — HA_URL/HA_TOKEN; none — nothing configured.
    source: Literal["ui", "env", "none"]
    url: Optional[str] = None
    token_set: bool = False
    #: The driver's own answer: connected / offline / not_configured.
    state: str
    #: Why it is offline: unreachable / unauthorized / http_<code>.
    error: Optional[str] = None
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None




# The closed list ck_peripherals_category enforces (migrations 029, 058). The
# SPA keys icons and actions off it, so a value here needs one there too.
PeripheralCategory = Literal["Lighting", "Access control", "Audio · Sensors",
                             "Automation", "Climate · Utilities"]
PERIPHERAL_CATEGORIES: tuple[str, ...] = get_args(PeripheralCategory)


class HaImportItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(min_length=3, max_length=255)
    #: Defaults to HA's friendly name.
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    #: Defaults from the entity's domain — see routers/homeassistant.py.
    category: Optional[PeripheralCategory] = None
    location: Optional[str] = Field(default=None, max_length=160)


class HaImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[HaImportItem] = Field(min_length=1, max_length=64)


class PendingAction(Base):
    """The follow-up half of a pulse, made durable.

    "Open the barrier for 8 seconds" is two commands across four workers. This
    row is written BEFORE the first half executes, so a crash between them
    leaves evidence rather than silence.

    DEFENCE IN DEPTH, NOT THE SAFETY MECHANISM. The PLC owns the authoritative
    timeout that closes the barrier (spec 11) and no deployment should depend on
    this instead of it. What this buys is that a deploy does not leave a gate
    open with no record of why.
    """

    __tablename__ = "pending_actions"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending','claimed','done','failed','abandoned')",
            name="ck_pending_state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    # NULL for a manual pulse from the Status panel — no alert behind it.
    alert_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("alerts.id", ondelete="CASCADE"),
        nullable=True)
    peripheral_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("peripherals.id", ondelete="CASCADE"),
        nullable=False)
    command: Mapped[str] = mapped_column(String(40), nullable=False)
    params: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                             nullable=False)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False,
                                          default=0, server_default="0")
    state: Mapped[str] = mapped_column(String(16), nullable=False,
                                       default="pending",
                                       server_default="pending")
    last_error: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    # "Hold until the zone is empty" (migration 080). NULL on an ordinary
    # pulse; on a hold, `due_at` is the safety cap and bridge/pending.py's
    # hold loop pulls it forward once the zone has been empty for the grace.
    hold_camera: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    hold_zone: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    hold_grace_s: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    empty_since: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)


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


# ─── ONVIF helpers ────────────────────────────────────────────────────────────

def onvif_host(ip: Optional[str], use_https: Optional[bool]) -> Optional[str]:
    """The host string every ONVIF call site should build its target from —
    never `camera.ip` directly.

    onvif-zeep (ONVIFCamera.get_definition) prepends "http://" to whatever host
    it's given UNLESS that string already starts with "http://"/"https://"; the
    port number plays no part in the decision. So a camera whose ONVIF service
    needs HTTPS (see Camera.onvif_use_https) needs an explicitly-scheme-prefixed
    host, not just port 443 — passing a bare IP with port=443 would still send
    plain HTTP bytes at a TLS listener.

    Takes plain values rather than a Camera so it works identically against the
    real ORM row and against the lightweight test fakes used for router wiring
    tests (SimpleNamespace-based; see test_sub_track_wiring.py).
    """
    if ip and use_https:
        return f"https://{ip}"
    return ip


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


# A DNS label set, per RFC 1123: cameras are usually addressed by IP here, but a
# site with DHCP reservations and internal DNS addresses them by name.
_HOSTNAME_VALID = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)


def validate_device_address(v: Optional[str]) -> Optional[str]:
    """A camera's own address for the ONVIF control plane — an IP or hostname.

    This is deliberately NOT a URL. `cameras.ip` is what every ONVIF call dials
    (maintenance, imaging, OSD, encoder, PTZ) and what the scanner dedupes on,
    and the commonest way to break it is to paste in something that carries a
    scheme, a port or a path — all of which produce an unreachable target that
    only shows up later as a connection refused against the wrong host.
    """
    if v is None:
        return None
    v = v.strip()
    if not v:
        return None
    try:  # an IP literal, normalised (e.g. "192.168.002.155" is not stored as typed)
        return str(ipaddress.ip_address(v))
    except ValueError:
        pass
    if not _HOSTNAME_VALID.match(v):
        raise ValueError(
            "Not a device address — give the camera's own IP or hostname, with "
            "no scheme, port or path (e.g. 192.168.2.155)"
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
    # The camera's OWN address, for ONVIF. Defaults to the RTSP URL's host, which
    # is right for a camera addressed directly and wrong whenever the stream is
    # re-published by something else (a relay, an NVR channel, a bridge): that
    # URL's host is the re-publisher, which speaks no ONVIF. Supply it here —
    # including as an `ip` column in a bulk CSV — when the two differ.
    ip: Optional[str] = Field(
        default=None,
        description="Camera's own IP/hostname for ONVIF; defaults to the RTSP URL's host",
    )
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

    @field_validator("ip", mode="before")
    @classmethod
    def _device_address(cls, v: Optional[str]) -> Optional[str]:
        return validate_device_address(v)

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
    # How long the NVR keeps this camera's footage, in MINUTES. 0 resets to
    # the appliance default; omitted/None leaves it unchanged.
    retention_minutes: Optional[int] = Field(default=None, ge=0, le=3650 * 1440)
    # DEPRECATED day-spelling of the same field, still accepted so scripted
    # callers written against the pre-minutes API keep working. The router
    # converts it to minutes at the edge and never stores it; sending both is
    # rejected rather than resolved by a silent precedence rule, because a
    # caller that disagrees with itself about a deletion deadline should be
    # told so rather than have one of its two answers picked for it.
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

    @model_validator(mode="after")
    def _one_retention_unit(self) -> "CameraUpdate":
        if self.retention_minutes is not None and self.retention_days is not None:
            raise ValueError(
                "Send retention_minutes or retention_days, not both — they are "
                "the same field in two units and a caller that sets both is "
                "asserting two different deletion deadlines."
            )
        return self

    def resolved_retention_minutes(self) -> Optional[int]:
        """The requested retention in MINUTES, whichever unit it arrived in.

        None means "not being changed"; 0 keeps its long-standing meaning of
        "reset to the appliance default".
        """
        if self.retention_minutes is not None:
            return self.retention_minutes
        if self.retention_days is not None:
            return self.retention_days * 1440
        return None


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
        "effective_retention_minutes": tracks.sub_retention_minutes(
            camera, camera.retention_minutes),
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
    # Defaults FALSE for the same reason the column does: a payload that omits
    # the field must not read as opted in, or the SPA would draw AI as running
    # on a camera nobody switched it on for.
    search_indexing: bool = False
    search_domains: list[str] = Field(default_factory=lambda: ["person", "vehicles"])
    # NVR retention override in MINUTES; None = appliance default. The default
    # itself rides along so the UI can show the effective policy and warn
    # when a change would shorten it (footage deletion). `retention_label` is
    # the human spelling ("45 minutes" / "30 days") so that every surface which
    # merely DISPLAYS a period renders it the same way, rather than each one
    # re-deriving days and disagreeing about how to round a sub-day value.
    retention_minutes: Optional[int] = None
    retention_default_minutes: int = 30 * 1440
    retention_label: str = ""
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
    # Time-lapse switch (migration 057). Only the switch rides on the camera — the
    # settings are read through /timelapse/cameras/{id} by holders of
    # timelapse_view, so the Recording tab can say a camera is still capturing
    # stills without every camera payload carrying the whole config.
    timelapse_enabled: bool = False
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
    # Whether this camera's ONVIF service needs HTTPS (see Camera.onvif_use_https).
    onvif_use_https: bool = False
    # True only when the device itself answered "yes, I pan" (see the column
    # comment on Camera.ptz_capabilities). False covers BOTH "asked, it is a
    # fixed camera" and "never asked" — `ptz` below is what tells them apart,
    # and the UI must not print "fixed camera" off this boolean alone.
    ptz_capable: bool = False
    # The cached capability block, verbatim. None = never probed. Small, and
    # every surface that offers a PTZ control needs it to know WHICH controls
    # to offer, so it rides along with the camera rather than costing a second
    # request per camera.
    ptz: Optional[dict[str, Any]] = None
    # The camera's low-resolution second stream, or null when it has none or
    # none has been resolved yet. Enriched on the way out with
    # `effective_retention_minutes` — what the sub is ACTUALLY kept for once the
    # per-camera value, the appliance default and the clamp to the main have all
    # been applied. Without it a camera on the default shows a daily cost and no
    # total, because the browser cannot know the default or the clamp.
    sub_track: Optional[dict[str, Any]] = None

    @classmethod
    def from_orm(cls, camera: Camera, local_rtsp_url: str) -> "CameraResponse":
        from .config import settings  # local import — models must stay import-light

        from .services import retention_units  # noqa: PLC0415 — see above

        return cls(
            retention_default_minutes=retention_units.default_retention_minutes(),
            retention_label=retention_units.format_retention(camera.retention_minutes),
            onvif_capable=bool(camera.ip and camera.onvif_port and camera.enc_password),
            onvif_port=camera.onvif_port,
            onvif_use_https=bool(camera.onvif_use_https),
            ptz_capable=(camera.ptz_capabilities or {}).get("supported") is True,
            ptz=camera.ptz_capabilities,
            privacy_masks=camera.privacy_masks or [],
            sub_track=_sub_track_out(camera),
            recording_schedule=camera.recording_schedule,
            motion_detection=camera.motion_detection,
            search_indexing=camera.search_indexing,
            search_domains=list(camera.search_domains or []),
            motion_sensitivity=camera.motion_sensitivity,
            retention_minutes=camera.retention_minutes,
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
            timelapse_enabled=bool(getattr(camera, "timelapse_enabled", False)),
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
    # The codec itself. H.265 (HEVC) halves the bitrate but most browsers can't play it in this player (Firefox
    # never; Safari only 'hvc1'; Android only with a capable hardware decoder) — a live / sub stream meant for
    # browsers should be H264. Switching codec restarts the stream on the camera.
    encoding: Optional[Literal["H264", "H265"]] = Field(default=None, description="H264 or H265")

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
    # MINUTES to keep the sub's footage. None leaves it as-is (appliance default
    # on first enable). Clamped to the main's retention — a scrub track
    # outliving the footage it helps scrub is pure waste.
    retention_minutes: Optional[int] = Field(default=None, ge=1, le=3650 * 1440)
    # DEPRECATED day-spelling, converted at the edge. See CameraUpdate.
    retention_days: Optional[int] = Field(default=None, ge=1, le=3650)

    @model_validator(mode="after")
    def _one_retention_unit(self) -> "SubTrackBody":
        if self.retention_minutes is not None and self.retention_days is not None:
            raise ValueError(
                "Send retention_minutes or retention_days, not both."
            )
        return self

    def resolved_retention_minutes(self) -> Optional[int]:
        """The requested sub retention in MINUTES, whichever unit it arrived in."""
        if self.retention_minutes is not None:
            return self.retention_minutes
        if self.retention_days is not None:
            return self.retention_days * 1440
        return None


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
    search_aliases: Optional[list[str]] = Field(default=None, max_length=32)
    suggested_aliases: Optional[list[str]] = Field(default=None, max_length=32)

    @field_validator("search_aliases", "suggested_aliases")
    @classmethod
    def bounded_aliases(cls, values):
        if values is None:
            return None
        if any(not v.strip() or len(v) > 120 for v in values):
            raise ValueError("Aliases must contain 1–120 characters")
        return list(dict.fromkeys(v.strip() for v in values))

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
    # The repair path for a wrong device address: a camera registered by its
    # relay URL carries the relay's host in `ip`, so every ONVIF call lands on a
    # box with no ONVIF. Verified against the device before it is stored, so this
    # cannot replace a working address with a typo.
    ip: Optional[str] = Field(
        default=None,
        description="Set/correct the camera's own IP or hostname for ONVIF",
    )

    @field_validator("ip", mode="before")
    @classmethod
    def _device_address(cls, v: Optional[str]) -> Optional[str]:
        return validate_device_address(v)
    onvif_use_https: Optional[bool] = Field(
        default=None,
        description="Override / set whether ONVIF needs HTTPS (defaults to the stored value)",
    )


class OsdUpdateBody(BaseModel):
    """Partial update of one OSD overlay."""

    text: Optional[str] = Field(default=None, max_length=255)
    position: Optional[str] = Field(
        default=None, description="UpperLeft / UpperRight / LowerLeft / LowerRight / Custom"
    )
    date_format: Optional[str] = None
    time_format: Optional[str] = None


# ── PTZ ───────────────────────────────────────────────────────────────────────
#
# Every axis is the ONVIF normalised range -1..1 with the ONVIF sign convention:
# pan +1 = right, tilt +1 = UP, zoom +1 = tele. Bounds are enforced here rather
# than trusted from the browser — a velocity outside the generic space is a
# fault on some firmwares and an undefined-but-obeyed slew on others, and the
# second outcome is a camera that runs away on a typo'd client.
#
# `None` on an axis means "this axis is not part of the command" and is NOT the
# same as 0.0: onvif_ptz omits absent axes from the SOAP body entirely, because
# sending Zoom:{x:0} to a pan-only head is a fault on several vendors.

_AXIS = dict(default=None, ge=-1.0, le=1.0)


class PtzMoveBody(BaseModel):
    """Continuous move — the camera keeps going until Stop, the ONVIF Timeout,
    or the server-side watchdog (services/ptz_control). Held buttons re-issue
    this; they do not hold one request open."""

    pan: Optional[float] = Field(**_AXIS, description="Velocity, +1 = right")
    tilt: Optional[float] = Field(**_AXIS, description="Velocity, +1 = up")
    zoom: Optional[float] = Field(**_AXIS, description="Velocity, +1 = tele")


class PtzStepBody(BaseModel):
    """Relative move — a bounded nudge, used by the arrow steps and by
    click-to-centre (which converts a click offset into a pan/tilt fraction)."""

    pan: Optional[float] = Field(**_AXIS, description="Translation, +1 = full swing right")
    tilt: Optional[float] = Field(**_AXIS, description="Translation, +1 = full swing up")
    zoom: Optional[float] = Field(**_AXIS)
    speed: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class PtzAbsoluteBody(BaseModel):
    """Absolute move to a normalised position. Only offered when the camera
    advertises the absolute spaces — see Camera.ptz_capabilities."""

    pan: Optional[float] = Field(**_AXIS)
    tilt: Optional[float] = Field(**_AXIS)
    zoom: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    speed: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class PtzPresetBody(BaseModel):
    """Store the camera's CURRENT position as a preset. The camera holds the position (GotoPreset by token);
    VMS keeps a record of it (PtzPreset) that is re-synced from the camera on every read, so a change made in
    the camera's own web UI still shows. Omit `token` to create; pass one to overwrite in place."""

    name: str = Field(min_length=1, max_length=64)
    token: Optional[str] = Field(default=None, max_length=128)


class PtzGotoBody(BaseModel):
    speed: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class PtzAimBody(BaseModel):
    """Aim by degrees (services/ptz_calibrate.aim_target): `right`° right and `up`° up (negative = left / down)
    from `origin` — "current" (a step) or "home" (a view relative to Home). `up_from_level`: `up` is the
    elevation above level instead. Needs a calibration with measured angles."""

    right: float = Field(default=0.0, ge=-360.0, le=360.0)
    up: float = Field(default=0.0, ge=-180.0, le=180.0)
    origin: Literal["current", "home"] = "current"
    up_from_level: bool = False
    speed: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class PtzPresetPatch(BaseModel):
    """VMS-side preset details. A rename is VMS-only on purpose: renaming on the camera is ONVIF SetPreset,
    which ALSO re-stores the current position — a typo fix must never re-aim the preset."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    is_home: Optional[bool] = None
    sort_order: Optional[int] = Field(default=None, ge=0, le=10000)


class PtzCentreBody(BaseModel):
    """Click-to-centre / drag-to-zoom, as a point in the RENDERED frame: x, y in -1..1 from the centre
    (+x right, +y UP), and an optional zoom factor for a drawn box (2 = the box is half the frame). The server
    turns it into a move using the camera's calibration and its current position (services/ptz_calibrate)."""

    x: float = Field(ge=-1.0, le=1.0)
    y: float = Field(ge=-1.0, le=1.0)
    zoom_factor: Optional[float] = Field(default=None, ge=0.02, le=50.0)   # < 1 = zoom out (right-click)
    speed: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    # The picture the operator clicked on (JPEG, base64, ~480 px wide) — lets the server check where the spot
    # actually landed and correct once (services/ptz_calibrate.verify_centre). Optional: no frame, no check.
    frame: Optional[str] = Field(default=None, max_length=800_000)


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


# Registration quality gate, in normalized plan units. 0.02 of a plan's width
# is ~1 m on a 50 m site and ~2 m on a 100 m one, which brackets the 0.5-1 m
# ground-plane target in plan Section 22.2 for the site sizes this runs on.
# Deliberately a single normalized figure rather than a metre value: most plans
# carry no `calibration`, so metres are simply not available to check against,
# and a limit that silently stopped applying on uncalibrated plans would be
# worse than one that is approximate on all of them.
REGISTRATION_MAX_RMS = 0.02

# A homography has 8 degrees of freedom, so 4 point pairs is the minimum that
# DETERMINES one — but 5 is the minimum that lets anyone check it, and 5 is what
# is enforced.
#
# The reason is measured, not stylistic. At exactly 4 pairs the solve is exact:
# the fitted transform passes through every landmark, so `reprojection_error`
# comes back 0 *whatever the operator clicked*. Moving one pin a fifth of the
# plan away still reports 0.000000000000. That makes the quality gate below
# vacuous at 4 pairs — it would accept an arbitrarily wrong registration with a
# perfect score, and every zone projected through it would land somewhere else
# on the site. The fifth pair is not extra precision, it is the whole check.
#
# The upper bound is a sanity limit on payload size, not a modelling one.
REGISTRATION_MIN_PAIRS = 5
REGISTRATION_MAX_PAIRS = 32


class LandmarkPair(BaseModel):
    """One landmark the operator matched between a camera frame and the plan.

    All four values are normalized 0-1 — `fx`/`fy` in the camera frame, `mx`/`my`
    on the plan image — matching every other spatial value in this service, so
    nothing breaks when a plan is re-uploaded at a different resolution or a
    camera is switched to a differently sized substream.

    Landmarks must be picked at GROUND level (pole bases, fence-post feet,
    painted marks), because a homography is exact only for points on one plane
    and the ground is the plane every zone lives on. A roof corner fits the
    maths and then places zones metres away from where the operator meant.
    """

    model_config = ConfigDict(extra="forbid")

    fx: float = Field(ge=0.0, le=1.0)
    fy: float = Field(ge=0.0, le=1.0)
    mx: float = Field(ge=0.0, le=1.0)
    my: float = Field(ge=0.0, le=1.0)


class CameraRegistrationBody(BaseModel):
    """Register one camera view against one plan.

    `homography` is fitted in the browser, where the error can update live as
    the operator places pins and where this service avoids taking on a numeric
    dependency for a least-squares solve. It is NOT trusted on arrival: the
    router re-derives the reprojection error from `landmark_pairs` and rejects
    a degenerate transform however well it fits them (see services/homography).

    `preset_token` is `''` for a camera's fixed view, or an ONVIF preset token
    for one recallable view of a PTZ. Sending it for a camera with no PTZ
    support is refused by the router rather than stored as an orphan.
    """

    model_config = ConfigDict(extra="forbid")

    sitemap_id: int
    preset_token: str = Field(default="", max_length=64)
    landmark_pairs: list[LandmarkPair] = Field(
        min_length=REGISTRATION_MIN_PAIRS, max_length=REGISTRATION_MAX_PAIRS,
    )
    homography: list[list[float]]

    @field_validator("homography")
    @classmethod
    def _shape(cls, v: list[list[float]]) -> list[list[float]]:
        """Shape only. Whether the matrix is *usable* is a geometric question
        that needs the landmark pairs too, so it lives in the router beside the
        other rejections rather than being half-answered here."""
        if len(v) != 3 or any(len(row) != 3 for row in v):
            raise ValueError("homography must be a 3x3 matrix")
        return v


class CameraRegistrationResponse(BaseModel):
    """A stored registration as the Map tab reads it.

    `reprojection_error_m` is populated only when the sitemap carries
    `calibration`; it is None on an uncalibrated plan rather than 0, because a
    zero there would read as a perfect fit instead of an unanswerable question.
    """

    id: int
    camera_id: uuid.UUID
    sitemap_id: int
    preset_token: str
    homography: list[list[float]]
    landmark_pairs: list[LandmarkPair]
    reprojection_error: float
    reprojection_error_m: Optional[float] = None
    footprint: list[list[float]]
    calibrated_at: datetime
    calibrated_by: Optional[str] = None




class PeripheralIn(BaseModel):
    """Operator-supplied peripheral facts. Note what is absent: no state and no
    `last_seen`. Those are a bridge's to report — accepting them here would let
    the wall show a hand-typed "LOCKED" that nothing verified."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    category: PeripheralCategory
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
    category: Optional[PeripheralCategory] = None
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
    # Whether a binding exists and the bridge has CONFIRMED its address
    # resolves. The wall needs it to decide whether a control may be offered at
    # all, and it rides along here so the device list is one request rather than
    # one per tile.
    binding_verified: bool = False
    # The binding itself, so the wall can say WHERE a device lives and WHY it is
    # unverified. `binding_verified` alone leaves an operator with a disabled
    # control and no way to diagnose it — which reads as a broken product
    # rather than as a binding nobody has configured yet. Never carries the
    # secret: BindingOut has no such field.
    binding: Optional["BindingOut"] = None


# ── Automation rules (migration 039) ─────────────────────────────────────────
# What a rule is allowed to SAY, validated at the door. An action shape that is
# merely stored — because the column is JSONB and anything fits — becomes a rule
# that looks fully configured in the UI and silently never does a thing. So the
# union below is closed and discriminated, and an unknown `type` is a 422.


class PeripheralAction(BaseModel):
    """Switch a device. `peripheral` is a peripherals slug; nothing resolves it
    in P1 because there is no bridge, and the alert records `no_bridge`."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["peripheral"]
    peripheral: str = Field(min_length=1, max_length=120)
    command: str = Field(min_length=1, max_length=40)
    # A momentary pulse: hold `command` for this long, then send `then`.
    # With `until`, for_seconds is the hold's safety cap (default 600).
    for_seconds: Optional[int] = Field(default=None, ge=1, le=600)
    then: Optional[str] = Field(default=None, max_length=40)
    # Hold `command` until the zone that triggered the rule has been empty for
    # `grace_s` seconds, then send `then` (bridge/pending.py: hold).
    until: Optional[Literal["zone_empty"]] = None
    grace_s: Optional[int] = Field(default=None, ge=0, le=60)

    @model_validator(mode="after")
    def _a_pulse_needs_its_close(self) -> "PeripheralAction":
        # A barrier told to open for 8 seconds with nothing to send afterwards
        # is a barrier that stays open. Unstorable, rather than discovered on
        # site.
        if self.for_seconds is not None and not self.then:
            raise ValueError(
                "for_seconds needs `then`: a pulse with no follow-up command "
                "leaves the device held in its first state")
        if self.until is not None and not self.then:
            raise ValueError(
                "until needs `then`: a hold with no follow-up command never ends")
        return self


class WebhookAction(BaseModel):
    """Call out to a pre-registered URL. `url_ref` names a stored endpoint
    rather than carrying a URL, so a rule row can never become an SSRF
    primitive an operator typed."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["webhook"]
    url_ref: str = Field(min_length=1, max_length=120)


#: Discriminated on `type`. There is deliberately no email/SMS/push member —
#: see spec 4.3: notification transports are not P1, and offering one that
#: silently does nothing is worse than not offering it.
Action = Annotated[Union[PeripheralAction, WebhookAction], Field(discriminator="type")]


def check_scope_pairing(scope_kind: Optional[str], scope_value: Optional[str]) -> None:
    """Mirror of ck_alert_rules_scope_value, enforced here so the operator gets
    a message instead of an IntegrityError.

    A camera- or zone-scoped rule with no value matches nothing while looking
    completely configured, and an `any`-scoped rule carrying one is telling the
    reader something that is not true.
    """
    if scope_kind == "any" and scope_value is not None:
        raise ValueError(
            "an 'any' rule applies everywhere, so it must not name a scope_value")
    if scope_kind in ("camera", "zone") and not scope_value:
        raise ValueError(
            f"a {scope_kind}-scoped rule must name the {scope_kind} it watches, "
            f"or it matches nothing")


class AlertRuleIn(BaseModel):
    """A new rule, as an operator states it.

    NOTE WHAT IS ABSENT: `mode`. A rule is always created log_only and is armed
    through its own endpoint, which audits as `rule.armed`. This is the same
    exclusion PeripheralIn makes for `last_state`, for the same reason — a field
    that grants an authority must not be settable as a side effect of a write
    that looks like configuration.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    activity: str = Field(min_length=1, max_length=64)
    scope_kind: Literal["camera", "zone", "any"] = "any"
    scope_value: Optional[str] = Field(default=None, max_length=160)
    # NULL accepts events the detector scored no confidence for, which is a
    # different thing from 0.0 — see services/rules.py.
    min_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    conditions: dict[str, Any] = Field(default_factory=dict)
    cooldown_s: int = Field(default=60, ge=0, le=86400)
    max_event_age_s: int = Field(default=30, ge=1, le=86400)
    severity: Literal["info", "warning", "critical"] = "info"
    actions: list[Action] = Field(default_factory=list)
    active_hours: Optional[dict[str, Any]] = None
    enabled: bool = True

    @field_validator("name", "activity", "scope_value")
    @classmethod
    def _strip(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @model_validator(mode="after")
    def _scope_is_coherent(self) -> "AlertRuleIn":
        check_scope_pairing(self.scope_kind, self.scope_value)
        return self


class AlertRulePatch(BaseModel):
    """Partial update. Same exclusion as AlertRuleIn: `mode` is not here, and
    `extra="forbid"` means asking for it is a 422 rather than a field quietly
    dropped from a request that returns 200."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    activity: Optional[str] = Field(default=None, min_length=1, max_length=64)
    scope_kind: Optional[Literal["camera", "zone", "any"]] = None
    scope_value: Optional[str] = Field(default=None, max_length=160)
    min_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    conditions: Optional[dict[str, Any]] = None
    cooldown_s: Optional[int] = Field(default=None, ge=0, le=86400)
    max_event_age_s: Optional[int] = Field(default=None, ge=1, le=86400)
    severity: Optional[Literal["info", "warning", "critical"]] = None
    actions: Optional[list[Action]] = None
    active_hours: Optional[dict[str, Any]] = None
    enabled: Optional[bool] = None

    @field_validator("name", "activity", "scope_value")
    @classmethod
    def _strip(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None


class AlertRuleOut(BaseModel):
    """A rule as the SPA sees it. `mode` is read-only here and everywhere."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    enabled: bool
    mode: str
    activity: str
    scope_kind: str
    scope_value: Optional[str] = None
    min_confidence: Optional[float] = None
    conditions: dict[str, Any] = Field(default_factory=dict)
    cooldown_s: int
    max_event_age_s: int
    severity: str
    # Plain `list`, not `list[Action]`: a rule stored before the union gained or
    # lost a member must stay READABLE. Validating on the way out would make an
    # old row a 500 on a screen whose whole job is to show it to somebody.
    actions: list[Any] = Field(default_factory=list)
    active_hours: Optional[dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime
    created_by: Optional[str] = None


class AlertOut(BaseModel):
    """An alert as the trigger log shows it.

    `notified` rides along because it is the only place that records what each
    action actually did — in P1 every peripheral action reads `no_bridge`, and
    the UI must be able to say so rather than implying the gate opened.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    rule_id: Optional[uuid.UUID] = None
    rule_name: Optional[str] = None
    event_ids: list[uuid.UUID] = Field(default_factory=list)
    camera_id: uuid.UUID
    severity: str
    state: str
    triggered_at: datetime
    acknowledged_at: Optional[datetime] = None
    acknowledged_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None
    resolution: Optional[str] = None
    resolution_note: Optional[str] = None
    notified: dict[str, Any] = Field(default_factory=dict)
    legal_hold: bool = False


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




# ─── Data Collection (migration 045) ──────────────────────────────────────────
#
# A DATASET IS A RECIPE, NOT A FOLDER. The row says "camera X, 10:00-12:00, 200
# frames"; collecting evaluates it. Raising the target and re-collecting
# re-samples the same window more finely, which is why the window and target are
# columns rather than something inferred from whatever files exist.

class Dataset(Base):
    """One training-image dataset: a sampling recipe plus its latest outcome.

    Deletable on purpose — this is working material for model training, not an
    evidence package. See migration 045 for the full reasoning, including why
    `camera_slug` is not a foreign key.
    """

    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")

    # The recipe.
    camera_slug: Mapped[str] = mapped_column(Text, nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    target_frames: Mapped[int] = mapped_column(Integer, nullable=False)
    jpeg_quality: Mapped[int] = mapped_column(Integer, nullable=False, default=85)

    # Optional detection filter (migration 052). With `model_id` set, the window
    # is scanned every `scan_interval_seconds` and only frames containing one of
    # `target_classes` are kept; `target_frames` then caps frames KEPT.
    model_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    model_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    target_classes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    min_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    scan_interval_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    frames_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # PTZ camera: sample only while it was confirmed AT these presets (ptz_presets.id as text; migration 068).
    # NULL / empty = the whole window.
    ptz_preset_ids: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)

    # The outcome. `frames` is the manifest the export reads — one
    # {filename, timestamp, sha256, bytes} per extracted image.
    frames: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    frames_collected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    frames_missing: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    frames_dir: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bytes_total: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    build_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Where the frames were pushed, once a push succeeded.
    labelstudio_project_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    labelstudio_task_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    labelstudio_exported_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    s3_bucket: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    s3_prefix: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    s3_uploaded_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    s3_uploaded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)


def dataset_interval_seconds(
    window_start: datetime, window_end: datetime, target_frames: Optional[int],
) -> Optional[float]:
    """Seconds between consecutive sampled frames, or None if unanswerable.

    THE ONE SOURCE OF TRUTH FOR THIS NUMBER. It is what actually tells an
    operator how far apart their training images are, and it is derived here —
    server-side, from the same inputs dataset_build.plan_timestamps uses — so
    the figure the UI prints and the spacing the sampler produces cannot drift
    apart. The SPA must never recompute it from the window and the target.
    """
    if not target_frames or target_frames <= 0:
        return None
    span = (window_end - window_start).total_seconds()
    if span <= 0:
        return None
    return round(span / target_frames, 3)


class DatasetCreateBody(BaseModel):
    """Mirrors the CHECK constraints in migration 045, so a bad request is a
    422 from the edge rather than a 500 out of Postgres."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = None
    camera_slug: str = Field(min_length=1, max_length=64)
    window_start: datetime
    window_end: datetime
    # 20000 is the DB ceiling too. At ~1.3 s per frame and 4-way concurrency
    # that is nearly two hours of extraction — see migration 045.
    target_frames: int = Field(default=200, ge=1, le=20000)
    jpeg_quality: int = Field(default=85, ge=1, le=95)
    # Detection filter. Omit model_id for plain evenly-spaced sampling.
    model_id: Optional[str] = Field(default=None, max_length=64)
    target_classes: list[str] = Field(default_factory=list, max_length=100)
    min_confidence: Optional[float] = Field(default=None, gt=0, lt=1)
    scan_interval_seconds: Optional[float] = Field(default=None, ge=0.5, le=3600)
    # PTZ camera: only frames while it was confirmed at these presets (their VMS ids). Empty = the whole window.
    ptz_preset_ids: list[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _window_ok(self):
        if self.window_start >= self.window_end:
            raise ValueError("window_start must be earlier than window_end")
        if self.model_id and not self.target_classes:
            raise ValueError("choose at least one class for the model to detect")
        return self


class DatasetUpdateBody(BaseModel):
    """Every field optional — PATCH semantics. The window check only fires when
    the update leaves both ends set; the router re-validates against the stored
    row so patching one end alone cannot invert the window."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = None
    camera_slug: Optional[str] = Field(default=None, min_length=1, max_length=64)
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    target_frames: Optional[int] = Field(default=None, ge=1, le=20000)
    jpeg_quality: Optional[int] = Field(default=None, ge=1, le=95)
    model_id: Optional[str] = Field(default=None, max_length=64)
    target_classes: Optional[list[str]] = Field(default=None, max_length=100)
    min_confidence: Optional[float] = Field(default=None, gt=0, lt=1)
    scan_interval_seconds: Optional[float] = Field(default=None, ge=0.5, le=3600)
    ptz_preset_ids: Optional[list[str]] = Field(default=None, max_length=200)


class DatasetResponse(BaseModel):
    """A dataset as the SPA sees it.

    Deliberately WITHOUT the `frames` manifest: at 200-20000 entries it would
    dominate every list payload, and the page only needs it when someone opens
    one dataset. GET /api/datasets/{id}/frames serves it on demand.
    """

    id: int
    name: str
    description: Optional[str] = None
    status: str
    camera_slug: str
    window_start: datetime
    window_end: datetime
    target_frames: int
    jpeg_quality: int
    # Derived server-side; never recomputed by the client.
    interval_seconds: Optional[float] = None
    frames_collected: int
    frames_missing: int
    model_id: Optional[str] = None
    model_name: Optional[str] = None
    target_classes: list[str] = Field(default_factory=list)
    min_confidence: Optional[float] = None
    scan_interval_seconds: Optional[float] = None
    frames_scanned: int = 0
    ptz_preset_ids: list[str] = Field(default_factory=list)
    frames_dir: Optional[str] = None
    bytes_total: Optional[int] = None
    build_error: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    labelstudio_project_id: Optional[int] = None
    labelstudio_task_count: Optional[int] = None
    labelstudio_exported_at: Optional[datetime] = None
    s3_bucket: Optional[str] = None
    s3_prefix: Optional[str] = None
    s3_uploaded_count: Optional[int] = None
    s3_uploaded_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: "Dataset") -> "DatasetResponse":
        return cls(
            id=row.id, name=row.name, description=row.description, status=row.status,
            camera_slug=row.camera_slug,
            window_start=row.window_start, window_end=row.window_end,
            target_frames=row.target_frames, jpeg_quality=row.jpeg_quality,
            interval_seconds=(row.scan_interval_seconds if row.model_id else
                              dataset_interval_seconds(
                                  row.window_start, row.window_end, row.target_frames)),
            frames_collected=row.frames_collected, frames_missing=row.frames_missing,
            model_id=row.model_id, model_name=row.model_name,
            target_classes=list(row.target_classes or []),
            min_confidence=row.min_confidence,
            scan_interval_seconds=row.scan_interval_seconds,
            frames_scanned=row.frames_scanned or 0,
            ptz_preset_ids=[str(x) for x in (row.ptz_preset_ids or [])],
            frames_dir=row.frames_dir, bytes_total=row.bytes_total,
            build_error=row.build_error, created_by=row.created_by,
            created_at=row.created_at, updated_at=row.updated_at,
            labelstudio_project_id=row.labelstudio_project_id,
            labelstudio_task_count=row.labelstudio_task_count,
            labelstudio_exported_at=row.labelstudio_exported_at,
            s3_bucket=row.s3_bucket, s3_prefix=row.s3_prefix,
            s3_uploaded_count=row.s3_uploaded_count, s3_uploaded_at=row.s3_uploaded_at,
        )


class DatasetExportBody(BaseModel):
    """Where to send a collected dataset. DESTINATIONS ONLY, NEVER CREDENTIALS.

    The bucket is chosen per import rather than read from settings so one
    appliance can feed several datasets to different destinations without a
    redeploy; the AWS keys stay in the server environment and never round-trip
    through a browser.

    Both destinations are opt-OUT. They serve different purposes — Label Studio
    so the frames can be annotated, S3 so they are kept somewhere durable — and
    either can fail on its own, so an operator must be able to re-run just the
    half that failed instead of re-uploading everything to fix it.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: Optional[int] = Field(default=None, ge=1)
    s3_bucket: Optional[str] = Field(default=None, max_length=63)
    s3_prefix: Optional[str] = Field(default=None, max_length=512)
    to_labelstudio: bool = True
    to_s3: bool = True


# ── Annotation classes and annotations (migration 047) ──────────────────────


class LabelClass(Base):
    """The fleet label taxonomy an administrator owns.

    Retired, never deleted — see migration 047. `key` is what exports carry and
    what annotations name, so it is immutable once created; `label` is the
    display name and is freely editable.
    """

    __tablename__ = "label_classes"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    color: Mapped[str] = mapped_column(String(9), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False)


class Annotation(Base):
    """One operator-drawn box: an index over the JPEG the NVR holds.

    The pixels and the JSON sidecar live on the NVR volume. This row is the
    searchable index over them, and the sidecar is what makes a missing row
    recoverable — see `services/annotation_store.adopt_orphans`.
    """

    __tablename__ = "annotations"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    camera_slug: Mapped[str] = mapped_column(Text, nullable=False)
    class_key: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    bbox_x: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_y: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_w: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_h: Mapped[float] = mapped_column(Float, nullable=False)
    frame_width: Mapped[int] = mapped_column(Integer, nullable=False)
    frame_height: Mapped[int] = mapped_column(Integer, nullable=False)
    image_path: Mapped[str] = mapped_column(Text, nullable=False)
    mask_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    dataset_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # Why this one is kept past its deadline (migration 048). Null alongside a
    # null expires_at means "a dataset owns it" — a different, equally valid
    # answer to the same question the posture report asks.
    kept_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)


class LabelClassCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    label: str = Field(min_length=1, max_length=120)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    sort_order: int = 0
    description: Optional[str] = Field(default=None, max_length=500)


class LabelClassUpdateBody(BaseModel):
    """`key` is absent on purpose — it is what annotations name and what
    exports carry, so renaming it would rewrite history. extra=forbid turns an
    attempt into a 422 rather than a silent no-op."""

    model_config = ConfigDict(extra="forbid")

    label: Optional[str] = Field(default=None, min_length=1, max_length=120)
    color: Optional[str] = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    sort_order: Optional[int] = None
    active: Optional[bool] = None
    description: Optional[str] = Field(default=None, max_length=500)


class LabelClassResponse(BaseModel):
    key: str
    label: str
    color: str
    sort_order: int
    active: bool
    description: Optional[str] = None
    annotation_count: int = 0

    @classmethod
    def from_row(cls, row: "LabelClass",
                 annotation_count: int = 0) -> "LabelClassResponse":
        return cls(key=row.key, label=row.label, color=row.color,
                   sort_order=row.sort_order, active=row.active,
                   description=row.description,
                   annotation_count=annotation_count)


class AnnotationResponse(BaseModel):
    id: str
    camera_slug: str
    class_key: str
    captured_at: datetime
    bbox: dict[str, float]
    frame: dict[str, int]
    image_path: str
    dataset_id: Optional[int] = None
    expires_at: Optional[datetime] = None
    kept_reason: Optional[str] = None
    created_by: Optional[str] = None

    @classmethod
    def from_row(cls, row: "Annotation") -> "AnnotationResponse":
        return cls(
            id=row.id.hex, camera_slug=row.camera_slug,
            class_key=row.class_key, captured_at=row.captured_at,
            bbox={"x": row.bbox_x, "y": row.bbox_y,
                  "width": row.bbox_w, "height": row.bbox_h},
            frame={"width": row.frame_width, "height": row.frame_height},
            image_path=row.image_path, dataset_id=row.dataset_id,
            expires_at=row.expires_at, kept_reason=row.kept_reason,
            created_by=row.created_by)


class AnnotationUpdateBody(BaseModel):
    """The two things that can change about a saved annotation.

    `class_key` fixes a misclick. That matters more than it sounds: the frame
    was captured at pointer-down and the footage behind it may have aged out,
    so without a relabel the only remedy is delete — which destroys a sample
    that cannot be redrawn.

    `keep` clears the expiry. `kept_reason` travels with it because
    `posture.derived_stills` reports how many stills are held with no deadline,
    and a number nobody has to justify is a number nobody reads.
    """

    model_config = ConfigDict(extra="forbid")

    class_key: Optional[str] = Field(default=None, max_length=64)
    keep: Optional[bool] = None
    kept_reason: Optional[str] = Field(default=None, max_length=500)


class AnnotationExportBody(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    camera: Optional[str] = None
    class_keys: Optional[list[str]] = None
    from_: Optional[datetime] = Field(default=None, alias="from")
    to: Optional[datetime] = None
    s3_bucket: Optional[str] = Field(default=None, max_length=63)
    s3_prefix: Optional[str] = Field(default=None, max_length=512)


# ── QA review sink (migration 053) ──────────────────────────────────────────


class QaSinkConfig(Base):
    """Where operator-drawn annotations go for external human review.

    SINGLETON — `id` is pinned to 1 by a check constraint. See migration 053
    for why the destination is one row rather than one per camera.

    THE TWO TOPIC COLUMNS ARE NOT THE SAME THING and the names are the only
    warning the next reader gets. `kafka_topic` is the Kafka topic this
    appliance produces to, and it must be one the remote consumer subscribes
    to. `payload_topic` is a string carried INSIDE the message that the remote
    side uses to decide how to treat the event. Setting one to the other's
    value produces a sink that reports success and delivers nothing.
    """

    __tablename__ = "qa_sink_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    delivery_method: Mapped[str] = mapped_column(String(16), nullable=False, default="kafka")
    message_format: Mapped[str] = mapped_column(String(32), nullable=False, default="review_legacy")
    webhook_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    webhook_token_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    bootstrap_servers: Mapped[str] = mapped_column(Text, nullable=False, default="")
    kafka_topic: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload_topic: Mapped[str] = mapped_column(
        Text, nullable=False, default="vms_annotation")
    severity: Mapped[str] = mapped_column(String(32), nullable=False, default="low")

    sasl_mechanism: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    sasl_username: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sasl_password_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    use_tls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    s3_bucket: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    s3_prefix: Mapped[str] = mapped_column(Text, nullable=False, default="")
    public_base_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Where the short clip of each annotation goes (migration 062). Null or
    # empty means no video is published, which is how every sink behaved before
    # this column existed.
    s3_video_prefix: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # The sink's OWN S3 credentials (migration 054). All three null means "use
    # the environment", which is what every appliance did before they existed —
    # see the migration for why the dataset mirror's key is the wrong one to
    # reuse here.
    aws_access_key_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    aws_secret_access_key_enc: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True)
    aws_region: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    class_map: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    updated_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)


class QaPublishOutbox(Base):
    """One annotation waiting to reach the QA queue, or the record that it did.

    `annotation_id` is the primary key and a CASCADE foreign key, which is what
    makes at-most-once enqueueing and privacy-correct deletion structural
    rather than remembered. `payload` is frozen at enqueue time so a retry
    sends what the first attempt sent, even if the config changed in between.
    """

    __tablename__ = "qa_publish_outbox"

    annotation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("annotations.id", ondelete="CASCADE"), primary_key=True)
    destination: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # The objects this publish put in the bucket — the frame as captured, and
    # the copy with the box burned on. Kept so deletion has something to
    # delete: without them the images outlive every record of why they exist.
    # `labelled_image_key` is null on rows published before migration 060,
    # which had only the one object.
    image_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    labelled_image_key: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True)
    # The clip, when one was stored (migration 062). Null when the video prefix
    # was off at enqueue, or the NVR had no footage and the message went out
    # without one.
    video_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)


class QaSinkConfigBody(BaseModel):
    """What an administrator can set from Administration -> Annotation Publishing.

    `sasl_password` is write-only: it arrives here and is never returned by the
    GET, the same contract camera credentials have. Omitting it on an update
    leaves the stored one alone — otherwise every unrelated edit to the class
    map would silently clear the broker password.
    """

    model_config = ConfigDict(extra="forbid")

    delivery_method: Literal["kafka", "webhook"] = "kafka"
    message_format: Literal["review_legacy", "annotation_v1"] = "review_legacy"
    webhook_url: Optional[str] = Field(default=None, max_length=2048)
    webhook_token: Optional[str] = Field(default=None, max_length=4096, exclude=True, repr=False)

    @field_validator("webhook_url")
    @classmethod
    def _webhook_url(cls, value):
        if not value:
            return None
        from urllib.parse import urlsplit
        value = value.strip()
        try:
            parsed = urlsplit(value)
            parsed.port
        except ValueError:
            raise ValueError("Enter a valid HTTPS webhook URL") from None
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or parsed.query
                or any(c.isspace() or ord(c) < 32 for c in value)):
            raise ValueError("Webhook URL must use HTTPS without credentials, query parameters or fragments")
        return value

    @field_validator("webhook_token")
    @classmethod
    def _webhook_token(cls, value):
        if value and any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise ValueError("Bearer token must contain printable ASCII without whitespace")
        return value

    enabled: bool = False
    bootstrap_servers: str = Field(default="", max_length=1000)
    kafka_topic: str = Field(default="", max_length=255)
    payload_topic: str = Field(default="vms_annotation", min_length=1, max_length=255)
    severity: str = Field(default="low", max_length=32)

    sasl_mechanism: Optional[Literal["PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"]] = None
    sasl_username: Optional[str] = Field(default=None, max_length=255)
    sasl_password: Optional[str] = Field(default=None, max_length=512)
    use_tls: bool = False

    s3_bucket: Optional[str] = Field(default=None, max_length=63)
    s3_prefix: str = Field(default="", max_length=512)
    public_base_url: Optional[str] = Field(default=None, max_length=512)
    # Blank turns video publishing off.
    s3_video_prefix: Optional[str] = Field(default=None, max_length=512)

    # BOTH halves of the S3 credential are write-only and behave exactly like
    # `sasl_password`: omitted means "keep the stored one", empty string means
    # "clear it". The key ID is hidden alongside the secret rather than shown,
    # because it names a live credential — it is the half an attacker needs to
    # know WHICH key to go looking for, and a screenshot of this form should
    # not identify the account's keys.
    aws_access_key_id: Optional[str] = Field(default=None, max_length=128)
    aws_secret_access_key: Optional[str] = Field(default=None, max_length=256)
    aws_region: Optional[str] = Field(default=None, max_length=64)

    class_map: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _enabled_needs_a_destination(self) -> "QaSinkConfigBody":
        """Refuse to switch on a sink that cannot possibly deliver.

        Every one of these is silent at runtime if it is missing: no brokers
        means the producer never connects, no topic means messages go nowhere,
        no bucket means the reviewer opens a broken image. An operator who ticks
        "enabled" on a blank form should be told now, not discover it from an
        outbox that never drains.
        """
        if not self.enabled:
            return self
        missing = [
            name for name, value in (
                ("brokers", self.bootstrap_servers.strip() if self.delivery_method == "kafka" else "unused"),
                ("Kafka topic", self.kafka_topic.strip() if self.delivery_method == "kafka" else "unused"),
                ("webhook URL", self.webhook_url if self.delivery_method == "webhook" else "unused"),
                ("S3 bucket", (self.s3_bucket or "").strip()),
            ) if not value
        ]
        if missing:
            raise ValueError(
                "Cannot enable annotation publishing without " + ", ".join(missing) + ".")
        return self

    @field_validator("class_map")
    @classmethod
    def _sane_class_map(cls, value: dict[str, str]) -> dict[str, str]:
        for key, mapped in value.items():
            if not re.fullmatch(r"[a-z0-9_]{1,64}", key or ""):
                raise ValueError(
                    f"'{key}' is not a label class key — keys are lowercase "
                    f"letters, digits and underscores.")
            if not (mapped or "").strip() or len(mapped) > 120:
                raise ValueError(
                    f"The subcategory for '{key}' must be 1 to 120 characters.")
        return value


class QaSinkConfigResponse(BaseModel):
    """The config as the browser sees it. NEVER carries the SASL password.

    `sasl_password_set` is the half of that secret it is safe to show: the form
    needs to render "a password is stored" differently from "no password", and
    a boolean answers that without the value leaving the appliance.
    """

    delivery_method: Literal["kafka", "webhook"] = "kafka"
    message_format: Literal["review_legacy", "annotation_v1"] = "review_legacy"
    webhook_url: Optional[str] = None
    webhook_token_set: bool = False
    enabled: bool
    bootstrap_servers: str
    kafka_topic: str
    payload_topic: str
    severity: str
    sasl_mechanism: Optional[str] = None
    sasl_username: Optional[str] = None
    sasl_password_set: bool = False
    use_tls: bool = False
    s3_bucket: Optional[str] = None
    s3_prefix: str = ""
    public_base_url: Optional[str] = None
    s3_video_prefix: Optional[str] = None
    # Two booleans, no values. Same contract as `sasl_password_set`: the form
    # has to distinguish "stored" from "not set" for each half, and a boolean
    # answers that without either half leaving the appliance.
    aws_access_key_id_set: bool = False
    aws_secret_access_key_set: bool = False
    aws_region: Optional[str] = None
    # True when this sink has no credentials of its own and is therefore
    # signing with the appliance's environment key. The form says so out loud:
    # otherwise two blank fields look like a misconfiguration rather than the
    # supported default.
    aws_uses_environment: bool = True
    class_map: dict[str, str] = Field(default_factory=dict)
    updated_by: Optional[str] = None
    updated_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: Optional["QaSinkConfig"]) -> "QaSinkConfigResponse":
        # A missing row is a real, expected state on a fresh appliance — the
        # defaults below ARE the answer, not a placeholder for one.
        if row is None:
            return cls(enabled=False, bootstrap_servers="", kafka_topic="",
                       payload_topic="vms_annotation", severity="low")
        return cls(
            delivery_method=row.delivery_method or "kafka",
            message_format=row.message_format or "review_legacy",
            webhook_url=row.webhook_url, webhook_token_set=bool(row.webhook_token_enc),
            enabled=row.enabled, bootstrap_servers=row.bootstrap_servers,
            kafka_topic=row.kafka_topic, payload_topic=row.payload_topic,
            severity=row.severity, sasl_mechanism=row.sasl_mechanism,
            sasl_username=row.sasl_username,
            sasl_password_set=bool(row.sasl_password_enc),
            use_tls=row.use_tls, s3_bucket=row.s3_bucket,
            s3_prefix=row.s3_prefix, public_base_url=row.public_base_url,
            s3_video_prefix=row.s3_video_prefix,
            aws_access_key_id_set=bool((row.aws_access_key_id or "").strip()),
            aws_secret_access_key_set=bool(row.aws_secret_access_key_enc),
            aws_region=row.aws_region,
            aws_uses_environment=not (row.aws_access_key_id or "").strip(),
            class_map=dict(row.class_map or {}), updated_by=row.updated_by,
            updated_at=row.updated_at)


# Optional outbound event integrations. Credentials stay encrypted in snapshots.
class EventDestination(Base):
    __tablename__ = "event_destinations"
    __table_args__ = (CheckConstraint("kind IN ('webhook','telegram','slack','kafka','email','sftp')"),)
    id: Mapped[uuid.UUID] = mapped_column(PGUUID, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    secret_enc: Mapped[Optional[str]] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    media: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_insecure: Mapped[bool] = mapped_column(Boolean, default=False)
    rate_per_min: Mapped[int] = mapped_column(Integer, default=60)
    max_age_s: Mapped[Optional[int]] = mapped_column(Integer)
    next_send_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EventSubscription(Base):
    __tablename__ = "event_subscriptions"
    id: Mapped[uuid.UUID] = mapped_column(PGUUID, primary_key=True, default=uuid.uuid4)
    destination_id: Mapped[uuid.UUID] = mapped_column(PGUUID, ForeignKey("event_destinations.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    types: Mapped[list] = mapped_column(ARRAY(String))
    filters: Mapped[dict] = mapped_column(JSONB, default=dict)
    cooldown_s: Mapped[Optional[int]] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EventOutbox(Base):
    __tablename__ = "event_outbox"
    __table_args__ = (Index("ix_event_unrouted", "seq", postgresql_where=text("routed_at IS NULL")),)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_id: Mapped[uuid.UUID] = mapped_column(PGUUID, unique=True)
    type: Mapped[str] = mapped_column(String(40))
    subject_id: Mapped[str] = mapped_column(String(80))
    camera_id: Mapped[Optional[uuid.UUID]] = mapped_column(PGUUID)
    envelope: Mapped[dict] = mapped_column(JSONB)
    routes: Mapped[list] = mapped_column(JSONB, default=list)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    routed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # The pull feed's cursor. Stamped by the (serialised) router, so it commits
    # in order and a reader can never skip a row — see migration 066.
    feed_seq: Mapped[Optional[int]] = mapped_column(BigInteger)


class EventDelivery(Base):
    __tablename__ = "event_deliveries"
    __table_args__ = (UniqueConstraint("outbox_seq", "destination_id"),
        CheckConstraint("state IN ('pending','sending','sent','retrying','failed','expired','suppressed')"),
        Index("ix_event_due", "state", "next_attempt_at"),
        Index("ix_event_destination_queue", "destination_id", "outbox_seq", postgresql_where=text("state IN ('pending','retrying','sending')")))
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    outbox_seq: Mapped[int] = mapped_column(BigInteger, ForeignKey("event_outbox.seq", ondelete="CASCADE"))
    destination_id: Mapped[uuid.UUID] = mapped_column(PGUUID, ForeignKey("event_destinations.id", ondelete="CASCADE"))
    destination: Mapped[dict] = mapped_column(JSONB)
    subscription_ids: Mapped[list] = mapped_column(ARRAY(PGUUID))
    state: Mapped[str] = mapped_column(String(12), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lease_owner: Mapped[Optional[str]] = mapped_column(String(64))
    lease_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    provider_ref: Mapped[Optional[str]] = mapped_column(String(120))
    media: Mapped[Optional[str]] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class EventCooldown(Base):
    __tablename__ = "event_cooldowns"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EventIntegrationSettings(Base):
    __tablename__ = "event_integration_settings"
    __table_args__ = (CheckConstraint("id = 1"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    feed_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    purged_through: Mapped[int] = mapped_column(BigInteger, default=0)
    # Base of playback links; NULL falls back to EVENTS_PUBLIC_BASE_URL (067).
    public_base_url: Mapped[Optional[str]] = mapped_column(Text)
    # The site mail server every email destination sends through (068). The
    # password is encrypted apart from the JSON so it is never echoed back.
    smtp: Mapped[Optional[dict]] = mapped_column(JSONB)
    smtp_secret_enc: Mapped[Optional[str]] = mapped_column(Text)


class EventMedia(Base):
    """The snapshot taken for one event, once, reused by every retry (068)."""
    __tablename__ = "event_media"
    outbox_seq: Mapped[int] = mapped_column(BigInteger, ForeignKey("event_outbox.seq", ondelete="CASCADE"), primary_key=True)
    #: ready | skipped | unavailable | spool_full
    state: Mapped[str] = mapped_column(String(16))
    source: Mapped[Optional[str]] = mapped_column(String(24))
    caption: Mapped[Optional[str]] = mapped_column(Text)
    captured_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    sha256: Mapped[Optional[str]] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
