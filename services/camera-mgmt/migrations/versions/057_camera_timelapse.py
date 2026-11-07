"""Time-lapse: a per-camera switch + settings, the stills it captures, the videos rendered from them.

Revision ID: 057
Revises: 056
Create Date: 2026-10-12

OFF BY DEFAULT, like every per-camera capture added since migration 038: the
column is NOT NULL DEFAULT false, so every existing camera comes out of this
migration exactly as it went in — recording, and nothing else.

Two tables, two lives:
  * timelapse_frames  — one row per still, bytes in the object store (object_key)
    or inline (image) when no store is configured; `expires_at` stamped at insert
    from the camera's time-lapse retention so the sweep is an indexed DELETE.
  * timelapse_renders — the render queue AND its results; status moves
    queued → running → done | failed.

Both cascade with the camera row. The objects in the store do not cascade by
themselves — services/timelapse.purge_camera removes them on camera delete.

Existence-guarded like 053-056 (see backend/migrate.py): an appliance that runs
this twice through the lineage repair must not fail on objects it already has.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "057"
down_revision: Union[str, None] = "056"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _insp():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _insp().has_table(name)


def _has_column(table: str, column: str) -> bool:
    return _has_table(table) and any(c["name"] == column for c in _insp().get_columns(table))


def upgrade() -> None:
    if not _has_column("cameras", "timelapse_enabled"):
        op.add_column("cameras", sa.Column("timelapse_enabled", sa.Boolean(), nullable=False,
                                           server_default=sa.text("false")))
    if not _has_column("cameras", "timelapse_config"):
        op.add_column("cameras", sa.Column("timelapse_config", postgresql.JSONB(), nullable=False,
                                           server_default=sa.text("'{}'::jsonb")))

    if not _has_table("timelapse_frames"):
        op.create_table(
            "timelapse_frames",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("camera_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False),
            sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("local_day", sa.Date(), nullable=False),
            sa.Column("object_key", sa.Text(), nullable=True),
            sa.Column("image", sa.LargeBinary(), nullable=True),
            sa.Column("bytes", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("width", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("height", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("brightness", sa.Float(), nullable=False, server_default=sa.text("0")),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.text("now()")),
        )
        op.create_index("ux_timelapse_frames_capture", "timelapse_frames",
                        ["camera_id", "captured_at"], unique=True)
        op.create_index("ix_timelapse_frames_camera_day", "timelapse_frames", ["camera_id", "local_day"])
        op.create_index("ix_timelapse_frames_expires_at", "timelapse_frames", ["expires_at"])

    if not _has_table("timelapse_renders"):
        op.create_table(
            "timelapse_renders",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("camera_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False, server_default="manual"),
            sa.Column("range_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("range_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("fps", sa.SmallInteger(), nullable=False, server_default=sa.text("24")),
            sa.Column("overlay", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("deflicker", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
            sa.Column("progress", sa.Float(), nullable=False, server_default=sa.text("0")),
            sa.Column("frames", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("object_key", sa.Text(), nullable=True),
            sa.Column("video", sa.LargeBinary(), nullable=True),
            sa.Column("bytes", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
            sa.Column("encoder", sa.String(32), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("requested_by", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.text("now()")),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("status IN ('queued','running','done','failed')",
                               name="ck_timelapse_renders_status"),
            sa.CheckConstraint("kind IN ('manual','daily')", name="ck_timelapse_renders_kind"),
        )
        op.execute("CREATE INDEX ix_timelapse_renders_camera_time ON timelapse_renders (camera_id, created_at DESC)")
        op.create_index("ix_timelapse_renders_status", "timelapse_renders", ["status"])


def downgrade() -> None:
    op.drop_table("timelapse_renders")
    op.drop_table("timelapse_frames")
    op.drop_column("cameras", "timelapse_config")
    op.drop_column("cameras", "timelapse_enabled")
