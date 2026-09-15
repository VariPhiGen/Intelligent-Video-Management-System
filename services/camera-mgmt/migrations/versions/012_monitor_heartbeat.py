"""Dead-man's switch for the health monitor.

Revision ID: 012
Revises: 011
Create Date: 2026-07-21

Single-row table the health monitor stamps every poll cycle (~30s). At
startup, a beat far in the past proves the service died WITHOUT running its
shutdown hook (power cut, OOM kill, GPU hang) — the span since the last beat
is then backfilled as 'unknown' in camera_status_events so uptime math never
counts unmonitored time as camera uptime. See services/health.py
(startup_mark_unknown). Postgres durability is the whole point: the last
beat written before a crash survives it on disk.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "monitor_heartbeat",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("beat_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("monitor_heartbeat")
