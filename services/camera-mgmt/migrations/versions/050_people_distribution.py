"""people distribution: zone occupancy samples

WHY A TABLE AND NOT `analytics_events`. An event is something that HAPPENED --
a zone entry, a fall, a plate read -- and the Events page is a list of things to
act on. Occupancy is a LEVEL: "three people are in Zone 1", true until it isn't.
Filing levels as events would put one row per zone per 30 seconds into the
operator's incident list for ever, and none of them would be actionable.

So this is a time series: one row per camera per sample, with the zones as
JSONB rather than a row each. The zones of one sample are read together, always,
and a zone is not an entity with a life of its own -- it is named by the
operator's activity config and can be renamed or removed there. Storing them as
a column of a sample means a rename never orphans anything and a read of "what
did this camera look like at 14:00" is one row.

WHAT IS NOT HERE. The activity's existing per-person dwell/exit records still go
to Kafka (`office_people_distribution`) exactly as before; this migration adds
nothing to that path and takes nothing from it. The two answer different
questions -- "who stayed how long" versus "how many are here now" -- and the
live panel can only be built from the second.

Revision ID: 050
Revises: 049
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "050"
down_revision: Union[str, None] = "049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "people_distribution_samples",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("camera_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False),
        # The producer's own clock, as the heatmap and the events path both do:
        # the moment the count was TRUE, not the moment it arrived.
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_count", sa.Integer, nullable=False, server_default="0"),
        # [{zone_id, zone_name, current_count, dwell_seconds_max, dwell_seconds_avg}]
        sa.Column("zones", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("source", sa.String(32), nullable=False, server_default="deepstream"),
        # Samples are cheap but endless; retention sweeps on this.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    # The only two reads: "this camera's latest" (live) and "this camera across
    # a day" (history). One index serves both.
    op.create_index("ix_pd_samples_camera_time", "people_distribution_samples",
                    ["camera_id", "captured_at"])
    op.create_index("ix_pd_samples_expires", "people_distribution_samples",
                    ["expires_at"])
    # A retried post of the same sample must not become a second row.
    op.create_unique_constraint("ux_pd_samples_camera_captured",
                                "people_distribution_samples",
                                ["camera_id", "captured_at"])


def downgrade() -> None:
    op.drop_constraint("ux_pd_samples_camera_captured",
                       "people_distribution_samples", type_="unique")
    op.drop_index("ix_pd_samples_expires", table_name="people_distribution_samples")
    op.drop_index("ix_pd_samples_camera_time", table_name="people_distribution_samples")
    op.drop_table("people_distribution_samples")
