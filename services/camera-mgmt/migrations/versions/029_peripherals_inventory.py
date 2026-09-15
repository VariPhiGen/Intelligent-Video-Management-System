"""Real peripheral inventory + a foreign-keyed HA placement table.

Revision ID: 029
Revises: 028
Create Date: 2026-08-24

Peripherals were a hardcoded array in the SPA (peripheralsData.ts), so the ids
in `sitemaps.ha_devices` (migration 028) pointed at demo data with no table
behind them — a pin could reference a device that no longer existed and the
map had to skip it defensively.

This replaces both halves:

  • `peripherals` — the operator's real device inventory. Only facts a person
    can actually know are writable: name, category, where it is, who makes it,
    and the address a future bridge will bind to. `last_state` / `last_seen`
    are deliberately NOT operator-editable; they stay NULL until an
    integration reports them, so the UI can say "state unknown" instead of
    showing a hand-typed status as if it were live.

  • `sitemap_ha_placements` — the pin, as a real row with foreign keys both
    ways and ON DELETE CASCADE. Deleting a plan or a device now removes its
    pins as a database guarantee rather than an application sweep.

`sitemaps.ha_devices` is dropped: every id it held referenced the demo array,
so there is nothing to migrate into the new table.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "029"
down_revision: Union[str, None] = "028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The three groupings the UI renders and colours by. A CHECK rather than a free
# string: the Map pin, the tile icon and the (future) action verb are all keyed
# off this, so an unknown category would render as a blank device.
CATEGORIES = ("Lighting", "Access control", "Audio · Sensors")


def upgrade() -> None:
    op.create_table(
        "peripherals",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(120), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("location", sa.String(160), nullable=True),
        sa.Column("vendor", sa.String(120), nullable=True),
        # Where a bridge will find it: an HA entity id, MQTT topic or relay
        # address. Free text — we do not yet know which integration lands.
        sa.Column("external_id", sa.String(200), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        # Integration-owned. NULL = never reported; the UI renders "unknown".
        sa.Column("last_state", sa.String(40), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.CheckConstraint(
            "category IN ('Lighting', 'Access control', 'Audio · Sensors')",
            name="ck_peripherals_category",
        ),
    )
    # Same case-insensitive uniqueness cameras use for names.
    op.create_index(
        "ix_peripherals_slug", "peripherals", ["slug"], unique=True,
    )
    op.execute(
        "CREATE UNIQUE INDEX ix_peripherals_name_lower ON peripherals (lower(name))"
    )

    op.create_table(
        "sitemap_ha_placements",
        sa.Column("sitemap_id", sa.BigInteger(), nullable=False),
        sa.Column("peripheral_id", sa.BigInteger(), nullable=False),
        sa.Column("x", sa.Float(), nullable=False),
        sa.Column("y", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["sitemap_id"], ["sitemaps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["peripheral_id"], ["peripherals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("sitemap_id", "peripheral_id"),
        sa.CheckConstraint("x >= 0 AND x <= 1 AND y >= 0 AND y <= 1",
                           name="ck_ha_placement_normalized"),
    )

    op.drop_column("sitemaps", "ha_devices")


def downgrade() -> None:
    from sqlalchemy.dialects import postgresql
    op.add_column("sitemaps", sa.Column("ha_devices", postgresql.JSONB(), nullable=True))
    op.drop_table("sitemap_ha_placements")
    op.drop_index("ix_peripherals_name_lower", table_name="peripherals")
    op.drop_index("ix_peripherals_slug", table_name="peripherals")
    op.drop_table("peripherals")
