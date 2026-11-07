"""PTZ presets get a permanent identity: history points at ptz_presets.id, not the camera's token.

A preset's token is just the camera's slot number, and cameras hand a deleted preset's number to the next preset
saved — so anything that remembered "token 3" silently re-bound to a different view (patrol steps, the position
history, per-preset AI zones next). ptz_position_log gains preset_id, back-filled from today's presets by token.

Revision ID: 071
Revises: 070
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "071"
down_revision: Union[str, None] = "070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("ptz_position_log")}
    if "preset_id" not in cols:
        op.add_column("ptz_position_log", sa.Column("preset_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.execute("""
        UPDATE ptz_position_log l SET preset_id = p.id
          FROM ptz_presets p
         WHERE l.preset_id IS NULL AND l.preset_token IS NOT NULL
           AND p.camera_id = l.camera_id AND p.token = l.preset_token
    """)


def downgrade() -> None:
    op.drop_column("ptz_position_log", "preset_id")
