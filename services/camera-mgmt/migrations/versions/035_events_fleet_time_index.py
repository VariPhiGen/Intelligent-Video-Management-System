"""Index analytics_events for the fleet-wide newest-first read.

Revision ID: 035
Revises: 034
Create Date: 2026-09-13

The Events tab opens on a live ticker of the latest events across EVERY camera,
refreshed every few seconds. 022 indexed events per camera
(`camera_id, started_at DESC`) and per activity, but not by time alone, so that
read would sort the whole retained table to return ten rows.

`id DESC` rides along because the reads order by (started_at, id) — a keyset
boundary that stays stable when two events share a timestamp — and an index
that stops at started_at would leave that tie-break to a sort.

No data change. On an appliance where the table is already large, build it
with CREATE INDEX CONCURRENTLY by hand before upgrading; alembic runs inside a
transaction, where CONCURRENTLY is not allowed.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "035"
down_revision: Union[str, None] = "034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_events_started_at",
        "analytics_events",
        [sa.text("started_at DESC"), sa.text("id DESC")],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_events_started_at", table_name="analytics_events", if_exists=True)
