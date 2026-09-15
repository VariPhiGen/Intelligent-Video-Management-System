"""Per-camera motion detection option.

Revision ID: 008
Revises: 007
Create Date: 2026-07-12

motion_detection (default OFF) opts a camera into the motion service
(services/motion), which analyses the camera's RELAY stream — no extra
camera connection, no credentials. motion_sensitivity is a preset name
(low / medium / high); NULL means the service default. Enforced by the
registry→motion reconcile loop (services/motion_client.py), mirroring the
NVR recording sync.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column("motion_detection", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "cameras",
        sa.Column("motion_sensitivity", sa.String(16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cameras", "motion_sensitivity")
    op.drop_column("cameras", "motion_detection")
