"""Per-camera opt-out for Smart Search indexing.

Revision ID: 030
Revises: 029
Create Date: 2026-08-31

Until now the search index took every enabled camera. That was a deliberate
starting point — search covering only some cameras is worse than search covering
all of them, because a nil result reads as "this person was never recorded"
rather than "this camera was never indexed" — but it is not a policy an operator
can express, and there are real reasons to exclude a camera: a lens pointed at a
screen full of personal data, a corridor nobody investigates, or simply the
compute.

DEFAULT TRUE, and that matters on upgrade: every existing camera keeps being
indexed, so applying this migration changes nothing until somebody turns a
camera off. A default of false would silently empty the index of an appliance
that was working.

The honesty problem this creates is handled in the SPA, not here: with an opt-out
in place, "no results" is ambiguous, so Smart Search has to distinguish a camera
that found nothing from a camera it never looked at.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "030"
down_revision: Union[str, None] = "029"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column(
            "search_indexing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("cameras", "search_indexing")
