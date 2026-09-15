"""CMM analytics config per camera.

Revision ID: 013
Revises: 012
Create Date: 2026-07-24

analytics_config holds the Camera Motion Map: polygon zones (normalized 0–1
coords) plus an activity→zone mapping, authored in Config → Zones & Analytics.
It is the JSON contract an external DeepStream pipeline pulls; the VMS itself
runs no inference. Default '{}' = nothing configured.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column(
            "analytics_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("cameras", "analytics_config")
