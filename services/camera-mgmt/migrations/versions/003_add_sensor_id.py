"""Add sensor_id column to cameras

Revision ID: 003
Revises: 002
Create Date: 2026-05-08

sensor_id is the upstream UUID from the source CMM database that this RTSP
relay was built to mirror.  It is NOT unique — the same sensor UUID may appear
on multiple downstream servers — and is nullable for cameras added without a
sensor mapping.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column("sensor_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_cameras_sensor_id", "cameras", ["sensor_id"])


def downgrade() -> None:
    op.drop_index("ix_cameras_sensor_id", table_name="cameras")
    op.drop_column("cameras", "sensor_id")
