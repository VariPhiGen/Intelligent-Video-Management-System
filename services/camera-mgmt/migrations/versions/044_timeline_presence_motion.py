"""timeline_presence also holds motion.

Revision ID: 044
Revises: 043
Create Date: 2026-09-23

MOTION IS A PRESENCE, NOT AN EVENT. The motion service's incident log is a
state machine (confirm, trigger, fixed cooldown) held in memory: its length is
the cooldown, not the motion, and it is gone after a restart. What playback
needs is the same thing 043 stores for people and vehicles — the stretches of
footage in which something moved — so motion runs go in the same table, the
same bounded segments, the same retention and erasure, under kind 'motion'.

Only the kind CHECK changes. Every other rule of 043 already fits.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "044"
down_revision: Union[str, None] = "043"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_timeline_presence_kind", "timeline_presence", type_="check")
    op.create_check_constraint("ck_timeline_presence_kind", "timeline_presence",
                               "kind IN ('person','vehicle','motion')")


def downgrade() -> None:
    op.execute("DELETE FROM timeline_presence WHERE kind = 'motion'")
    op.drop_constraint("ck_timeline_presence_kind", "timeline_presence", type_="check")
    op.create_check_constraint("ck_timeline_presence_kind", "timeline_presence",
                               "kind IN ('person','vehicle')")
