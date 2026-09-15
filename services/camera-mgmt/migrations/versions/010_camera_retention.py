"""Per-camera NVR retention override.

Revision ID: 010
Revises: 009
Create Date: 2026-07-20

retention_days is how long the NVR keeps this camera's footage before the
hourly retention pass deletes it. NULL means the appliance default
(NVR_DEFAULT_RETENTION_DAYS). The value is pushed to the NVR when changed
and re-asserted by the registry→NVR reconcile loop after an NVR restart
(services/nvr_client.py), mirroring the recording sync.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column("retention_days", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cameras", "retention_days")
