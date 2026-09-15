"""HA device placement on sitemaps (Map tab → HA Devices layer).

Revision ID: 028
Revises: 027
Create Date: 2026-08-24

The HA Devices layer drew its pins from a hash of the device id, so every
plan showed the same six devices in the same meaningless spots and there was
no way to add or remove one. Placement now lives here: an optional
`ha_devices` JSONB list of `{id, x, y}` (x/y normalized 0–1, same convention
as camera placement in camera_metadata and as `calibration`). NULL / empty =
nothing placed on this plan, which is the honest state for a fresh map.

`id` is the peripheral's id — today that comes from the peripherals demo data
(peripheralsData.ts); when a real Home-Assistant bridge lands those become HA
entity ids and this column needs no change.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "028"
down_revision: Union[str, None] = "026"   # was "027"; that revision moved to the compliance extension
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sitemaps",
        sa.Column("ha_devices", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sitemaps", "ha_devices")
