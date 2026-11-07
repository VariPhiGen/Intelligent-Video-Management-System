"""Per-preset AI: each PTZ preset is a fixed-camera view with its own zones, tripwires and rules.

ptz_presets gains
  * analytics_config — the same {regions, activities} shape as cameras.analytics_config, for this preset's view;
  * reference / reference_night (+ _at) — a full-size picture of the view, day and night (IR) apart: what zones
    are drawn on, and what an arrival is checked against (services/ptz_track). Refreshed by itself from confident
    matches, so a site that changes week by week keeps a current picture.
analytics_events gains preset_id / preset_name — which preset the camera was confirmed at when the event happened
("Gate · Zone 1"); NULL for fixed cameras.

Revision ID: 072
Revises: 071
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "072"
down_revision: Union[str, None] = "071"
branch_labels = None
depends_on = None


def _cols(table: str) -> set:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    have = _cols("ptz_presets")
    for name, col in (("analytics_config", postgresql.JSONB()), ("reference", sa.LargeBinary()),
                      ("reference_at", sa.DateTime(timezone=True)), ("reference_night", sa.LargeBinary()),
                      ("reference_night_at", sa.DateTime(timezone=True))):
        if name not in have:
            op.add_column("ptz_presets", sa.Column(name, col, nullable=True))
    have = _cols("analytics_events")
    if "preset_id" not in have:
        op.add_column("analytics_events", sa.Column("preset_id", postgresql.UUID(as_uuid=True), nullable=True))
    if "preset_name" not in have:
        op.add_column("analytics_events", sa.Column("preset_name", sa.String(64), nullable=True))


def downgrade() -> None:
    for name in ("preset_name", "preset_id"):
        op.drop_column("analytics_events", name)
    for name in ("reference_night_at", "reference_night", "reference_at", "reference", "analytics_config"):
        op.drop_column("ptz_presets", name)
