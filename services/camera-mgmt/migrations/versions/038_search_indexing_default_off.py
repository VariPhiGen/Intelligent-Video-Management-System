"""Smart Search indexing is opt-IN for newly added cameras.

Revision ID: 038
Revises: 037
Create Date: 2026-09-18

Migration 030 added `search_indexing` as an opt-OUT, defaulting true, and gave
the reason: search covering only some cameras is worse than search covering all
of them, because a nil result reads as "this person was never recorded" rather
than "this camera was never indexed".

That reasoning holds for an appliance whose fleet is already indexed. It does
not hold for the moment a camera is ADDED. Registering a camera is a recording
decision; indexing the people and vehicles in it is a second decision, with its
own CPU cost, its own disk cost and — because a person crop is personal data —
its own justification. Defaulting it true made the second decision silently,
for every camera an operator onboarded, and an operator who wanted AI on one
camera had to go and turn it OFF on all the others.

So the default moves. `motion_detection` has always been opt-in and this is now
the same shape: a new camera records and does nothing else until somebody asks
it to.

ONLY THE DEFAULT MOVES. This alters the column default for rows inserted from
here on and touches no existing row: an appliance whose cameras are indexed
today keeps indexing them, exactly as 030 was careful to do in the other
direction. Emptying a working index is not what this is for.

`search_domains` is deliberately left at person+vehicles. It is the stored
answer to "index what", and it means nothing while `search_indexing` is false —
keeping it is what makes switching indexing on a single click that works,
rather than a switch that appears to do nothing.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "038"
down_revision: Union[str, None] = "037"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "cameras",
        "search_indexing",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.false(),
    )


def downgrade() -> None:
    op.alter_column(
        "cameras",
        "search_indexing",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.true(),
    )
