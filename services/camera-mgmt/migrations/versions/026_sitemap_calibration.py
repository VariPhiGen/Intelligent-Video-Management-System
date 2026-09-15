"""Georeferencing calibration for sitemaps (GPS auto-placement).

Revision ID: 026
Revises: 025
Create Date: 2026-08-18

A floor-plan image has no inherent relationship to the Earth, so a camera's
GPS can't place it on the plan without calibration. This adds an optional
`calibration` JSONB column holding 2–3 control points
(`{x, y, lat, lng}`, x/y normalized 0–1) that anchor the image to real-world
coordinates. Two points give a similarity transform (translate + uniform
scale, north-up); a third solves rotation/shear. NULL = uncalibrated (manual
placement only, unchanged). Placement itself still lives in camera_metadata.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "026"
down_revision: Union[str, None] = "025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sitemaps",
        sa.Column("calibration", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sitemaps", "calibration")
