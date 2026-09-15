"""Per-camera groom-after override.

Revision ID: 011
Revises: 010
Create Date: 2026-07-20

groom_after_days is how many days a camera's footage stays full quality
before the NVR's nightly grooming pass remuxes it to keyframe-only "cold"
footage. NULL means the NVR's appliance default (recording.groom_after_days
in its cameras.yaml). Values >= the camera's retention effectively mean
"never groomed" — footage is deleted before it would be compressed. Pushed
to the NVR on change and re-asserted by the registry→NVR reconcile loop
(services/nvr_client.py), mirroring retention_days (migration 010).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column("groom_after_days", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cameras", "groom_after_days")
