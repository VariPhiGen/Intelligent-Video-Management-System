"""Camera status event log (uptime/downtime history).

Revision ID: 006
Revises: 005
Create Date: 2026-07-09

camera_status_events is an append-only transition log: the health monitor
inserts one row each time a camera's health_status actually changes
(connected / disconnected / error / unknown / disabled). Uptime graphs and
percentages are reconstructed from these transitions, so a stable camera
costs ~0 rows/day. Rows older than UPTIME_EVENTS_RETENTION_DAYS are pruned
by the monitor; camera deletion cascades.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "camera_status_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "camera_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("cameras.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_status_events_camera_time",
        "camera_status_events",
        ["camera_id", "changed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_status_events_camera_time", table_name="camera_status_events")
    op.drop_table("camera_status_events")
