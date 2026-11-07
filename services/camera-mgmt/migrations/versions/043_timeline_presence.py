"""timeline_presence: when a person or vehicle was in view, for playback markers.

Revision ID: 043
Revises: 042
Create Date: 2026-09-23

WHAT A ROW IS. One stretch of time during which one tracked object of one
kind (person / vehicle) was in a camera's view, as the analytics tracker saw
it. Long presences arrive as consecutive SEGMENTS of at most ten minutes (the
producer cuts at two), so a parked car is a run of rows, not one row that stays
open for a day. The playback timeline merges them back into bars at read time.

WHY NOT `analytics_events`. That table holds what an operator's CONFIGURED
activity decided happened, and services/ai_events.py refuses anything else on
purpose — raw detections there would turn the Events page into a detection
log. Presence is not a decision; it is where the footage has something in it.

WHY NOT searchdb. Search keeps the best crop of a track, filtered by the
indexing policy, with no duration; it can be absent altogether (search
profile off). Playback markers must not depend on either.

EVERY SEGMENT IS BOUNDED (the CHECK below). That bound is what lets the read
turn "overlaps [from, to)" into an index range on started_at: nothing that
started more than MAX_SEGMENT before `from` can reach into the window.

RETENTION is analytics_events': `expires_at` stamped at insert from the
camera's footage retention, swept with `DELETE WHERE expires_at < now()`.
Erasure (DSR, whole-camera purge) deletes rows directly — a marker over
footage that no longer exists would be a claim about something unverifiable.

`id` is minted by the producer (uuid5 of camera, track and segment start), so
a retried batch collapses on the primary key.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "043"
down_revision: Union[str, None] = "042"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "timeline_presence",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("camera_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("peak_confidence", sa.Float(), nullable=True),
        sa.Column("track_id", sa.String(64), nullable=True),
        # Detector class (car, truck, bus, …) — the kind is the coarse one.
        sa.Column("label", sa.String(32), nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="cpu"),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('person','vehicle')", name="ck_timeline_presence_kind"),
        sa.CheckConstraint(
            "ended_at >= started_at AND ended_at - started_at <= interval '10 minutes'",
            name="ck_timeline_presence_bounded"),
    )
    op.execute("CREATE INDEX ix_presence_camera_kind_time "
               "ON timeline_presence (camera_id, kind, started_at)")
    op.execute("CREATE INDEX ix_presence_expires_at ON timeline_presence (expires_at)")


def downgrade() -> None:
    op.drop_table("timeline_presence")
