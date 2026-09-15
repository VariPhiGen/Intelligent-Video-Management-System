"""The event spine: analytics detections, and the alerts raised from them.

Revision ID: 022
Revises: 021
Create Date: 2026-08-08

Until now the CMM authoring layer terminated in a void — a camera could be
configured to watch for `intrusion` and nothing persisted when it fired. This
adds the two tables everything downstream needs (Events page, notifications,
compound rules, occupancy dashboards, alert-quality reporting).

**Two tables, not one — deliberately.** A *detection* is what the pipeline
emitted; an *alert* is what a human is asked to look at. Conflating them breaks
three things we know are coming:

  1. Compound rules (the Verkada-style "person loitering AND no PPE") produce
     ONE alert from SEVERAL detections — an N:1 that a single table cannot
     express.
  2. Alert-quality reporting needs the detections that did NOT alert; if a
     detection is only stored once it raises an alert, the denominator is gone.
  3. The volume profiles differ by orders of magnitude, and so does retention —
     detections are bulk personal data on a short clock, alerts are a small
     working set that may sit under legal hold for years.

**Detections are personal data.** "Person at gate 3, 14:22, wearing red" is a
record about an identifiable individual and would otherwise outlive the footage
it was derived from. `analytics_events.expires_at` is therefore stamped at
INSERT from the owning camera's retention policy, so the row carries its own
deletion deadline rather than depending on a sweep to recompute policy later.
Retention becomes `DELETE WHERE expires_at < now()` on an indexed column, and
the lawful basis is inherited from the camera (migration 021) rather than
invented here.

Not partitioned. `expires_at` bounds the table's growth, which is the usual
reason to reach for partitioning; with the indexes below this holds to tens of
millions of rows. If a very large fleet outgrows it, converting to declarative
partitioning on `started_at` is the escalation — noted here because the table
rewrite it requires is much cheaper to plan for now than to discover later.

`alert_rules` and the FK from `alerts.rule_id` land in 023 with the rules
engine: the rule schema depends on how conditions get expressed, and guessing
that shape now would cost a re-migration. `rule_name` is denormalised onto
`alerts` so a historical alert still reads correctly after its rule is renamed
or deleted.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision: str = "022"
down_revision: Union[str, None] = "021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Detections ────────────────────────────────────────────────────────────
    op.create_table(
        "analytics_events",
        # UUID rather than the BIGSERIAL used by camera_status_events/audit_log,
        # deliberately: the producer is an EXTERNAL pipeline delivering
        # at-least-once. Letting it mint the id makes retries idempotent via
        # ON CONFLICT DO NOTHING, with no round-trip to allocate a key first.
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
        sa.Column(
            "camera_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("cameras.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Soft reference to analytics_activity_types.key — NOT a foreign key.
        # The pipeline exposes more activities than the catalog currently offers
        # (33 of 57 are still unselectable), and a detection from an
        # un-catalogued type must be storable rather than rejected at ingest.
        sa.Column("activity", sa.String(64), nullable=False),
        # Region name from the camera's analytics_config.regions. NULL = the
        # detector is whole-frame, which is a real configuration, not "unset".
        sa.Column("zone", sa.String(120), nullable=True),
        # Interval, not an instant: dwell/loitering/queue events have duration,
        # and half the dormant catalog is interval-shaped. ended_at NULL means
        # either a point event or one still open.
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        # 0..1. NULL when the detector reports none — distinct from 0.0.
        sa.Column("confidence", sa.Float(), nullable=True),
        # Tracker id from the pipeline. Only unique within a camera + tracker
        # session, so it is not a key — it is the seam cross-camera Re-ID would
        # later join on.
        sa.Column("track_id", sa.String(64), nullable=True),
        # Coarse class (person|vehicle|…) promoted out of attributes so the
        # common filter is a btree hit instead of a JSONB probe.
        sa.Column("object_class", sa.String(32), nullable=True),
        # Detector attributes verbatim — colour, PPE flags, plate text, …
        # This is what compound rules filter on, and precisely the shape we
        # cannot predict, so it stays schemaless. GIN-indexed below.
        sa.Column("attributes", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("snapshot_path", sa.Text(), nullable=True),
        # Stable key for cooldown collapse. A line-crossing on a busy corridor
        # emits thousands of rows an hour; the collapse strategy has to be
        # decided before the data exists, not after.
        sa.Column("dedupe_key", sa.String(128), nullable=True),
        # When the detection HAPPENED vs when we received it. Keeping both is
        # what makes pipeline lag and camera clock skew diagnosable at all.
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # deepstream|motion|onvif|manual. ONVIF Profile M metadata ingest would
        # arrive here without a schema change.
        sa.Column(
            "source", sa.String(32), nullable=False, server_default="deepstream"
        ),
        # Stamped at INSERT from the camera's retention policy. See the module
        # docstring: the row carries its own deletion deadline.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    # The Events page and every per-camera chart read this way.
    op.create_index(
        "ix_events_camera_time",
        "analytics_events",
        ["camera_id", sa.text("started_at DESC")],
    )
    # Fleet-wide "show me all ANPR hits" and the Tier 2 dashboards.
    op.create_index(
        "ix_events_activity_time",
        "analytics_events",
        ["activity", sa.text("started_at DESC")],
    )
    # Retention sweep: DELETE WHERE expires_at < now().
    op.create_index("ix_events_expires_at", "analytics_events", ["expires_at"])
    # Attribute filtering for compound rules.
    op.create_index(
        "ix_events_attributes",
        "analytics_events",
        ["attributes"],
        postgresql_using="gin",
    )
    # Partial: only rows that opted into dedupe pay for the index.
    op.create_index(
        "ix_events_dedupe",
        "analytics_events",
        ["dedupe_key", sa.text("started_at DESC")],
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )

    # ── Alerts ────────────────────────────────────────────────────────────────
    op.create_table(
        "alerts",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
        # FK to alert_rules deferred to 023 (see module docstring).
        sa.Column("rule_id", PGUUID(as_uuid=True), nullable=True),
        # Denormalised on purpose: a historical alert must still say what fired
        # it after the rule is renamed or deleted.
        sa.Column("rule_name", sa.String(120), nullable=True),
        # The detection(s) behind this alert. An array rather than a join table:
        # compound alerts reference a handful of events, never thousands, and
        # the GIN index answers "which alerts cite this event?" adequately
        # without a third table to keep in step.
        sa.Column(
            "event_ids",
            ARRAY(PGUUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        # Denormalised from the triggering event: every operator view filters by
        # camera, and joining through an array to get there would be absurd.
        sa.Column(
            "camera_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("cameras.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("severity", sa.String(16), nullable=False, server_default="info"),
        # new|acknowledged|assigned|resolved|dismissed
        sa.Column("state", sa.String(16), nullable=False, server_default="new"),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(255), nullable=True),
        sa.Column("assigned_to", sa.String(255), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(255), nullable=True),
        # true_positive|false_positive|duplicate|no_action. This single column
        # is what makes per-rule false-positive reporting possible; it cannot be
        # reconstructed after the fact, so it is here from the first row.
        sa.Column("resolution", sa.String(32), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        # Verification (VLM or human) — nullable now, populated when alert
        # verification lands. Cheap to carry; painful to backfill.
        sa.Column("verified_by", sa.String(16), nullable=True),   # vlm|human
        sa.Column("verdict", sa.String(16), nullable=True),       # confirmed|refuted|uncertain
        sa.Column("verdict_reason", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        # Per-transport delivery receipts: {"email": {...}, "webhook": {...}}.
        # Kept on the alert so "was anyone actually told?" is answerable without
        # correlating against service logs.
        sa.Column(
            "notified", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        # Evidence lock. While true, neither this alert nor the footage it cites
        # may be aged out. NOTE: the ON DELETE CASCADE above means deleting a
        # camera would still remove held alerts — blocking that is an app-layer
        # check that belongs with the router, not a constraint here.
        sa.Column(
            "legal_hold", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # The Events page default view: open alerts, newest first.
    op.create_index(
        "ix_alerts_state_time", "alerts", ["state", sa.text("triggered_at DESC")]
    )
    op.create_index(
        "ix_alerts_camera_time", "alerts", ["camera_id", sa.text("triggered_at DESC")]
    )
    # Per-rule tuning: volume, acknowledge latency, false-positive rate.
    op.create_index(
        "ix_alerts_rule_time",
        "alerts",
        ["rule_id", sa.text("triggered_at DESC")],
        postgresql_where=sa.text("rule_id IS NOT NULL"),
    )
    op.create_index("ix_alerts_event_ids", "alerts", ["event_ids"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_alerts_event_ids", table_name="alerts")
    op.drop_index("ix_alerts_rule_time", table_name="alerts")
    op.drop_index("ix_alerts_camera_time", table_name="alerts")
    op.drop_index("ix_alerts_state_time", table_name="alerts")
    op.drop_table("alerts")

    op.drop_index("ix_events_dedupe", table_name="analytics_events")
    op.drop_index("ix_events_attributes", table_name="analytics_events")
    op.drop_index("ix_events_expires_at", table_name="analytics_events")
    op.drop_index("ix_events_activity_time", table_name="analytics_events")
    op.drop_index("ix_events_camera_time", table_name="analytics_events")
    op.drop_table("analytics_events")
