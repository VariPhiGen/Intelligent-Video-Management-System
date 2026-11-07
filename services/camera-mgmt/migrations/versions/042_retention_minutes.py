"""Retention becomes minute-granular.

Revision ID: 042
Revises: 041
Create Date: 2026-09-22

`cameras.retention_days` becomes `cameras.retention_minutes`, and the sub
track's `retention_days` key inside `cameras.sub_track` becomes
`retention_minutes` with it.

A RENAME, NOT AN ADDITION, and that is the load-bearing decision here. A
second column alongside the first would have been a smaller diff, but
retention is not one number used in one place: the NVR's sweep, the Smart
Search index's `expires_at`, `analytics_events`, the heatmap buckets, the sub
track's clamp and the DPDP posture report all derive a deletion deadline from
it. Two columns means two of those can disagree, and the shape of that
disagreement is the appliance reporting a retention it is not keeping while a
sweep quietly deletes on the other one. There is one number.

The conversion is exact (`* 1440`) and loses nothing on the way up. The
downgrade is NOT exact: a camera set to 90 minutes cannot be spelled in whole
days, so downgrading floors it to 0 — which the old code reads as "appliance
default". That is the honest failure (footage kept LONGER than asked, visibly
at the default) rather than the alternative of rounding up to a day and
silently inventing a policy nobody set.

Nothing is deleted here and no deadline moves: `retention_minutes = days *
1440` is the same period expressed differently. The Smart Search index
restamps itself from the registry's next reconcile pass, which re-asserts
every camera's retention within `nvr_sync_interval` seconds of boot.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "042"
down_revision: Union[str, None] = "041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MINUTES_PER_DAY = 1440


def upgrade() -> None:
    op.alter_column(
        "cameras", "retention_days",
        new_column_name="retention_minutes",
        existing_type=sa.Integer(),
        existing_nullable=True,
    )
    op.execute(
        f"UPDATE cameras SET retention_minutes = retention_minutes * {MINUTES_PER_DAY} "
        "WHERE retention_minutes IS NOT NULL"
    )
    # The sub track's operator-set retention lives inside the sub_track JSONB
    # blob (migration 032), so it needs the same conversion. jsonb_exists()
    # rather than the `?` operator on purpose: `?` is a bind-parameter marker
    # to the driver and would make this statement unexecutable.
    op.execute(
        "UPDATE cameras SET sub_track = "
        "  (sub_track - 'retention_days') || "
        f"  jsonb_build_object('retention_minutes', (sub_track->>'retention_days')::int * {MINUTES_PER_DAY}) "
        "WHERE sub_track IS NOT NULL "
        "  AND jsonb_exists(sub_track, 'retention_days') "
        "  AND jsonb_typeof(sub_track->'retention_days') = 'number'"
    )
    # A sub track carrying a null retention_days: drop the dead key rather than
    # converting null into a number.
    op.execute(
        "UPDATE cameras SET sub_track = sub_track - 'retention_days' "
        "WHERE sub_track IS NOT NULL AND jsonb_exists(sub_track, 'retention_days')"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE cameras SET sub_track = "
        "  (sub_track - 'retention_minutes') || "
        f"  jsonb_build_object('retention_days', (sub_track->>'retention_minutes')::int / {MINUTES_PER_DAY}) "
        "WHERE sub_track IS NOT NULL "
        "  AND jsonb_exists(sub_track, 'retention_minutes') "
        "  AND jsonb_typeof(sub_track->'retention_minutes') = 'number'"
    )
    op.execute(
        "UPDATE cameras SET sub_track = sub_track - 'retention_minutes' "
        "WHERE sub_track IS NOT NULL AND jsonb_exists(sub_track, 'retention_minutes')"
    )
    # Integer division: sub-day retentions floor to 0, which the day-based code
    # reads as "appliance default". See the module docstring.
    op.execute(
        f"UPDATE cameras SET retention_minutes = retention_minutes / {MINUTES_PER_DAY} "
        "WHERE retention_minutes IS NOT NULL"
    )
    op.alter_column(
        "cameras", "retention_minutes",
        new_column_name="retention_days",
        existing_type=sa.Integer(),
        existing_nullable=True,
    )
