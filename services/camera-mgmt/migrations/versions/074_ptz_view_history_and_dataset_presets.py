"""PTZ phase 4: a dataset of a PTZ camera can take its frames only while the camera was AT chosen presets.

ptz_view_history — every change of a PTZ camera's CONFIRMED view (services/ptz_track: arrived at a preset and the
picture matched, or moving / off preset / not confirmed). Valkey keeps only the last 20 changes (enough to name the
preset of an event a moment old); a dataset samples a window from hours or days ago, so the confirmed views are kept
here too, for as long as the camera's position history (ptz_settings retention).

datasets.ptz_preset_ids — the presets (ptz_presets.id, as text) a dataset samples at; NULL / empty = the whole window,
as before. Frames are spread over the confirmed time at those presets only, never across a move between them, and
each frame records which preset it shows.

Revision ID: 074
Revises: 073
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "074"
down_revision: Union[str, None] = "073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "ptz_view_history" not in insp.get_table_names():
        op.create_table(
            "ptz_view_history",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("camera_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False),
            sa.Column("at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("preset_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("preset_token", sa.String(128), nullable=True),
            sa.Column("preset_name", sa.String(64), nullable=True),
            sa.Column("verified_by", sa.String(16), nullable=True),
        )
        op.create_index("ix_ptz_view_history_camera_at", "ptz_view_history", ["camera_id", "at"])
        op.create_index("ix_ptz_view_history_at", "ptz_view_history", ["at"])
    if "ptz_preset_ids" not in {c["name"] for c in insp.get_columns("datasets")}:
        op.add_column("datasets", sa.Column("ptz_preset_ids", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("datasets", "ptz_preset_ids")
    op.drop_index("ix_ptz_view_history_at", table_name="ptz_view_history")
    op.drop_index("ix_ptz_view_history_camera_at", table_name="ptz_view_history")
    op.drop_table("ptz_view_history")
