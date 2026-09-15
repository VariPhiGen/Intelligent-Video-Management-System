"""Purpose-bound retention: capture WHY a camera's retention is set as it is.

Revision ID: 020
Revises: 019
Create Date: 2026-08-07

DPDP requires retention to be purpose-bound, not "whatever the disk holds". The
audit log already records WHO changed retention and WHEN (retention.changed);
this column stores the WHY — the operator's justification — so a Fiduciary can
attest, per camera, why footage is kept for the period it is. Nullable: existing
cameras have no justification on file until next edited.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "020"
down_revision: Union[str, None] = "019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cameras", sa.Column("retention_justification", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("cameras", "retention_justification")
