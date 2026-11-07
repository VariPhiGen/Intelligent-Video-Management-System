"""PTZ phase 3: a preset's zones follow its view when the preset changes, and outlive its delete.

ptz_presets gains
  * analytics_review — set when the preset's VIEW changed under its zones (re-saved in VMS, or re-stored in the
    camera's own web page): {state, reason, at, by, offset, zoom_changed, previous_config}. state "shifted" = the
    zones were moved with the view and keep running; "review" = paused until someone checks them on the new picture;
    "awaiting_picture" = re-stored outside VMS, paused until the first confirmed arrival takes a picture to line up
    against (services/ptz_view_changes);
  * previous_reference — the view's picture from before the change, for the before/after check.
ptz_view_archive — the zones + rules + picture of a deleted preset (or of a missing one whose slot the camera gave to
a new preset), so they can be copied onto another preset instead of being lost. Rows stay until "Forget".

Revision ID: 073
Revises: 072
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "073"
down_revision: Union[str, None] = "072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    have = {c["name"] for c in sa.inspect(bind).get_columns("ptz_presets")}
    if "analytics_review" not in have:
        op.add_column("ptz_presets", sa.Column("analytics_review", postgresql.JSONB(), nullable=True))
    if "previous_reference" not in have:
        op.add_column("ptz_presets", sa.Column("previous_reference", sa.LargeBinary(), nullable=True))
    if "ptz_view_archive" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "ptz_view_archive",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("camera_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False),
            sa.Column("preset_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("token", sa.String(128), nullable=True),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("pan", sa.Float(), nullable=True),
            sa.Column("tilt", sa.Float(), nullable=True),
            sa.Column("zoom", sa.Float(), nullable=True),
            sa.Column("analytics_config", postgresql.JSONB(), nullable=False),
            sa.Column("picture", sa.LargeBinary(), nullable=True),
            sa.Column("reason", sa.String(255), nullable=True),
            sa.Column("archived_by", sa.String(255), nullable=True),
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_ptz_view_archive_camera", "ptz_view_archive", ["camera_id", "archived_at"])


def downgrade() -> None:
    op.drop_index("ix_ptz_view_archive_camera", table_name="ptz_view_archive")
    op.drop_table("ptz_view_archive")
    op.drop_column("ptz_presets", "previous_reference")
    op.drop_column("ptz_presets", "analytics_review")
