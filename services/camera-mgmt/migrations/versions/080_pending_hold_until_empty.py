"""pending actions: "hold until the zone is empty"

A rule action may now say: switch ON, and switch back OFF once the zone that
triggered it has been empty for a few seconds (a siren that sounds for as long
as someone is in the restricted zone). It reuses the pulse's durable follow-up
row, so everything that makes a pulse safe carries over:

  * the OFF is committed BEFORE the ON is sent;
  * `due_at` is a hard cap (the action's for_seconds, default 600): if the
    occupancy loop dies, the sweep still switches the device off then;
  * the hold loop only ever pulls `due_at` FORWARD to now — it can end a hold
    early, never extend one past its cap except by a new rule firing.

New columns, all NULL on ordinary pulses:
  hold_camera   which camera's occupancy decides it (slug)
  hold_zone     which zone on that camera (NULL = any zone)
  hold_grace_s  how long the zone must stay empty before the OFF
  empty_since   when the zone was first seen empty (NULL while occupied)

Revision ID: 080
Revises: 079
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "080"
down_revision: Union[str, None] = "079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pending_actions", sa.Column("hold_camera", sa.String(120), nullable=True))
    op.add_column("pending_actions", sa.Column("hold_zone", sa.String(160), nullable=True))
    op.add_column("pending_actions", sa.Column("hold_grace_s", sa.SmallInteger, nullable=True))
    op.add_column("pending_actions", sa.Column("empty_since", sa.DateTime(timezone=True),
                                               nullable=True))
    op.create_index("ix_pending_actions_holds", "pending_actions", ["peripheral_id"],
                    postgresql_where=sa.text("state = 'pending' AND hold_camera IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("ix_pending_actions_holds", table_name="pending_actions")
    for col in ("empty_since", "hold_grace_s", "hold_zone", "hold_camera"):
        op.drop_column("pending_actions", col)
