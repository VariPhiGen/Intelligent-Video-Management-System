"""PTZ presets kept in VMS, where each PTZ camera is pointing, and click-to-centre calibration.

Until now presets lived ONLY on the camera and VMS asked for them each time the PTZ tab opened, then forgot
them. KEC kec01, 2026-10-26: the operator wants to know which position every PTZ camera is on, and who moved
it when. So:

  * ptz_presets — VMS's record of each camera preset: the camera's token, a name (VMS's name wins for display:
    renaming on the camera means ONVIF SetPreset, which also re-stores the CURRENT position — a rename must
    never silently re-aim a preset), the stored pan/tilt/zoom, a thumbnail, Home, order, and whether the
    camera still lists it (`missing_on_camera`, so a camera reset is visible and the preset can be put back).
  * ptz_position_log — one row per CHANGE of where a camera points (at preset X / off-preset / moving /
    unknown) with who or what moved it. Change rows only, not every poll: a camera parked on a preset all
    night writes one row, not 2 880.
  * cameras.ptz_calibration — how far the picture moves per unit of RELATIVE pan/tilt at a few zoom levels,
    measured from the camera's own frames (services/ptz_calibrate). Click-to-centre used one fixed fraction
    of the WHOLE pan range whatever the zoom, so zoomed in it overshot by tens of degrees.

Revision ID: 069
Revises: 068
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "069"
down_revision: Union[str, None] = "068"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    tables = _tables()
    if "ptz_presets" not in tables:
        op.create_table(
            "ptz_presets",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("camera_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False),
            sa.Column("token", sa.String(128), nullable=False),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("pan", sa.Float(), nullable=True),
            sa.Column("tilt", sa.Float(), nullable=True),
            sa.Column("zoom", sa.Float(), nullable=True),
            sa.Column("is_home", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("thumbnail", sa.LargeBinary(), nullable=True),
            sa.Column("thumbnail_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("missing_on_camera", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_by", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("camera_id", "token", name="ux_ptz_presets_camera_token"),
        )
    if "ptz_position_log" not in tables:
        op.create_table(
            "ptz_position_log",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("camera_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False),
            sa.Column("at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("preset_token", sa.String(128), nullable=True),
            sa.Column("preset_name", sa.String(64), nullable=True),
            sa.Column("pan", sa.Float(), nullable=True),
            sa.Column("tilt", sa.Float(), nullable=True),
            sa.Column("zoom", sa.Float(), nullable=True),
            sa.Column("cause", sa.String(16), nullable=True),
            sa.Column("actor", sa.String(255), nullable=True),
        )
        op.create_index("ix_ptz_position_log_camera_at", "ptz_position_log", ["camera_id", "at"])
        op.create_index("ix_ptz_position_log_at", "ptz_position_log", ["at"])
    if "ptz_calibration" not in _columns("cameras"):
        op.add_column("cameras", sa.Column("ptz_calibration", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("cameras", "ptz_calibration")
    op.drop_index("ix_ptz_position_log_at", table_name="ptz_position_log")
    op.drop_index("ix_ptz_position_log_camera_at", table_name="ptz_position_log")
    op.drop_table("ptz_position_log")
    op.drop_table("ptz_presets")
