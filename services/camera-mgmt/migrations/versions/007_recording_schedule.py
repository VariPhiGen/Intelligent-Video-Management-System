"""Per-camera recording schedule.

Revision ID: 007
Revises: 006
Create Date: 2026-07-10

recording_schedule holds an optional calendar restricting WHEN the NVR records
this camera (Milestone-style). NULL = record 24/7 (the default / prior
behaviour). Shape:

  {"mode": "weekly",  "rules": [{"days": [0..6],  "start": "HH:MM", "end": "HH:MM"}]}
  {"mode": "monthly", "rules": [{"days": [1..31], "start": "HH:MM", "end": "HH:MM"}]}

weekly days are ISO weekdays (0=Monday); monthly days are days of the month.
Times are server-local (TZ env); end <= start wraps past midnight. Enforced by
the registry->NVR reconcile loop (services/nvr_client.py + services/schedule.py).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column("recording_schedule", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cameras", "recording_schedule")
