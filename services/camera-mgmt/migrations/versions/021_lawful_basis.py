"""Lawful basis as a first-class, validated field (DPDP purpose-binding).

Revision ID: 021
Revises: 020
Create Date: 2026-08-07

Lawful basis used to live in free-form camera_metadata that the backend never
interpreted — not validated, required or reportable. Promote it (plus the
purpose text and a notice/signage-posted flag) to real columns so a camera
cannot register without a documented lawful basis and purpose, and so the
posture report can attest them. retention_justification already landed in 020.

Existing registered cameras keep whatever lawful_basis string they carried in
metadata (back-filled here) even if it predates the current vocabulary — no data
loss; the closed-set validation only gates NEW registrations.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "021"
down_revision: Union[str, None] = "020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cameras", sa.Column("lawful_basis", sa.String(64), nullable=True))
    op.add_column("cameras", sa.Column("purpose", sa.Text(), nullable=True))
    op.add_column(
        "cameras",
        sa.Column("notice_posted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Back-fill from the free-form metadata so existing cameras keep their basis.
    # `->>` yields NULL when the key is absent, so the IS NOT NULL check both
    # guards the key's existence and avoids the JSONB `?` operator (which can
    # collide with a driver's param marker in a raw migration string).
    op.execute(
        "UPDATE cameras "
        "SET lawful_basis = camera_metadata->>'lawful_basis' "
        "WHERE lawful_basis IS NULL AND camera_metadata->>'lawful_basis' IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("cameras", "notice_posted")
    op.drop_column("cameras", "purpose")
    op.drop_column("cameras", "lawful_basis")
