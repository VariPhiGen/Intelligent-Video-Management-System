"""The Activity Type catalog follows the CPU activity registry.

Revision ID: 036
Revises: 035
Create Date: 2026-09-14

Until now `analytics_activity_types` was authored by hand: an administrator
could add a type by typing a name and invent its settings, and nothing tied
either to code that runs. The CPU analytics service now publishes its activity
registry (GET /activities): which activities exist, which can run, which zones
each takes, and every setting's definition. camera-mgmt copies that into this
table (services/activity_catalog.py), so the table becomes a synced record of
the registry plus what an administrator still owns — label, colour, order.

Five columns carry what the registry says:

  status              available | hold | unregistered. Only `available` can
                      be added to a camera. `unregistered` = the registry no
                      longer lists it; the row is KEPT, because stored camera
                      configs and historical events still name the key.
  zone_rule           optional (no zone → whole frame) | required | tripwire.
  description         the registry's one-line description.
  definition_version  the registry's version of that definition.
  synced_at           when camera-mgmt last copied it.

EVERY EXISTING ROW STARTS `unregistered` / `required`. Nothing here guesses what
the registry will say: the first sync (on the api's first analytics reconcile
pass, seconds after start) sets the real values. Until then no activity can be
added — which is exactly right on a deployment with no analytics service,
because nothing would run it.

No rows are inserted, deleted or rewritten, and no camera config is touched.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "036"
down_revision: Union[str, None] = "035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "analytics_activity_types"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("status", sa.String(16), nullable=False,
                                    server_default="unregistered"))
    op.add_column(_TABLE, sa.Column("zone_rule", sa.String(16), nullable=False,
                                    server_default="required"))
    op.add_column(_TABLE, sa.Column("description", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("definition_version", sa.Integer(), nullable=True))
    op.add_column(_TABLE, sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for column in ("synced_at", "definition_version", "description", "zone_rule", "status"):
        op.drop_column(_TABLE, column)
