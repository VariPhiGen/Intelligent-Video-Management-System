"""PTZ settings — how long movement history is kept (one row, id=1).

Patrol-heavy cameras write a history row on every step (about 6 a minute at 20 s stops) into BOTH the position
log and the patrol run log; at the old flat 90 days that is ~400 MB per camera patrolling all day (2026-10-27).
Operator moves are what an investigation needs ("who moved it"); a patrol's thousandth identical lap isn't. So
two retention periods: `history` = {"operator_days": 90, "patrol_days": 14} (services/ptz_track.retention()),
admin-editable on Cameras → PTZ. No row = those defaults.

Revision ID: 070
Revises: 069
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "070"
down_revision: Union[str, None] = "069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "ptz_settings" not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table(
            "ptz_settings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("history", postgresql.JSONB(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_by", sa.String(255), nullable=True),
        )
    # Patrol rows are pruned by cause; keep that delete off a full scan.
    op.execute("CREATE INDEX IF NOT EXISTS ix_ptz_position_log_cause_at ON ptz_position_log (cause, at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_ptz_position_log_cause_at")
    op.drop_table("ptz_settings")
