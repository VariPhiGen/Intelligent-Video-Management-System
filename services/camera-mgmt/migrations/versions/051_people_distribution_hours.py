"""people distribution: fixed clock-hour buckets of DISTINCT people

WHAT AN HOURLY NUMBER MEANS HERE: how many distinct tracked people were in that
zone at any point during that clock hour. Not detections, not frames, not the
sum of the 10-second samples, and not whoever happened to be standing there at
the end of the hour.

WHY THE TRACKER IDS ARE STORED AND NOT JUST A COUNT. A count cannot be unioned.
The pipeline reports every 10 seconds, so one person standing still for 40
minutes appears in ~240 consecutive samples; adding counts would report 240
people. Keeping the SET and taking its size is the only way the same person
counts once -- and it is also what makes the ingest idempotent, because a
retried or duplicated sample unions the same ids and changes nothing.

It is also what makes the hour boundary behave. A person present 06:55 -> 07:30
is reported in samples belonging to both hours, so their id lands in both sets
and they correctly count once in each. Nothing has to detect the boundary or
carry state across it: the sample's own timestamp decides which bucket it joins.

BUCKETS ARE FIXED CLOCK HOURS ON THE LOCAL CLOCK. An activity enabled at 06:30
contributes to 06:00-07:00 -- the bucket does not become 06:30-07:30. `hour_start`
is the floored local hour, stored as UTC like every other instant here.

ONE ROW PER (camera, hour, zone), which answers the only question the history
page asks -- "for this camera on this date, every hour and every zone" -- as one
indexed range scan, without a normalised zone table that a zone rename would
orphan.

Revision ID: 051
Revises: 050
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "051"
down_revision: Union[str, None] = "050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "people_distribution_hours",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("camera_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False),
        # The floored LOCAL hour, stored UTC. 06:00-07:00 local is one row.
        sa.Column("hour_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("zone_name", sa.String(128), nullable=False),
        # The set itself: ["ds:cam1-m7cx:4711", ...]. Its length is the answer.
        sa.Column("tracker_ids", postgresql.JSONB, nullable=False,
                  server_default="[]"),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The history read is exactly this range scan.
    op.create_index("ix_pd_hours_camera_hour", "people_distribution_hours",
                    ["camera_id", "hour_start"])
    op.create_index("ix_pd_hours_expires", "people_distribution_hours",
                    ["expires_at"])
    # What makes a repeated sample a union rather than a second row.
    op.create_unique_constraint(
        "ux_pd_hours_camera_hour_zone", "people_distribution_hours",
        ["camera_id", "hour_start", "zone_name"])


def downgrade() -> None:
    op.drop_constraint("ux_pd_hours_camera_hour_zone",
                       "people_distribution_hours", type_="unique")
    op.drop_index("ix_pd_hours_expires", table_name="people_distribution_hours")
    op.drop_index("ix_pd_hours_camera_hour", table_name="people_distribution_hours")
    op.drop_table("people_distribution_hours")
