"""Per-camera privacy masks.

Revision ID: 005
Revises: 004
Create Date: 2026-07-08

privacy_masks holds a list of polygons in NORMALIZED image coordinates:
[[[x, y], ...], ...] with 0 <= x,y <= 1, each polygon >= 3 points. The NVR
rasterizes them at the stream's native resolution and burns them into the
recording (per-camera opt-in re-encode; cameras without masks keep -c copy).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column("privacy_masks", JSONB, nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("cameras", "privacy_masks")
