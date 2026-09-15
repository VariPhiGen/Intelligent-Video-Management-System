"""Admin-managed CMM activity-type catalog.

Revision ID: 014
Revises: 013
Create Date: 2026-07-24

analytics_activity_types is the shared vocabulary of detection types the
DeepStream pipeline supports (PPE, Intrusion, …). Cameras' analytics_config
activities reference these by `key`. Previously hardcoded in code; now
admin-editable in Config → Zones & Analytics → Manage types. Seeded with the
original nine defaults so existing behaviour is preserved.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEFAULTS = [
    ("ppe", "PPE", "#ffb020"),
    ("fire", "Fire & Smoke", "#ef5350"),
    ("unauthorized", "Unauthorized Access", "#4fd1c5"),
    ("intrusion", "Intrusion", "#b388ff"),
    ("fall", "Fall Detection", "#52c77e"),
    ("crowd", "Crowd", "#ff8a65"),
    ("face", "Face Recognition", "#64b5f6"),
    ("phone", "Phone Usage", "#f06292"),
    ("vehicle", "Vehicle", "#a1887f"),
]


def upgrade() -> None:
    table = op.create_table(
        "analytics_activity_types",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("color", sa.String(length=9), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    )
    op.bulk_insert(table, [
        {"key": k, "label": lbl, "color": c, "sort_order": i}
        for i, (k, lbl, c) in enumerate(_DEFAULTS)
    ])


def downgrade() -> None:
    op.drop_table("analytics_activity_types")
