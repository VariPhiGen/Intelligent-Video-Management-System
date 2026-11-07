"""An annotation can be kept, and the keeping has to be answerable for.

Revision ID: 048
Revises: 047
Create Date: 2026-09-26

047 gave every loose annotation an `expires_at` and nothing a way to clear it.
`expires_at IS NULL` was reachable only by attaching the annotation to a
dataset, and nothing in the product ever set `dataset_id` — so in practice
EVERY annotation an operator drew was swept at ANNOTATION_TTL_MINUTES and the
exemption was unreachable. The feature deleted its own output.

The fix is not simply "let anyone clear the deadline". `posture.derived_stills`
reports `annotations_without_expiry` as a fact an operator has to answer for,
and a one-click forever button turns that number into noise. So keeping is a
recorded decision: WHY it is kept lands in this column, WHO kept it and WHEN
lands in the audit log (annotation.kept / annotation.released), and the posture
report keeps counting.

Nullable, with no default and no backfill: a null reason on a row that already
has no expiry means "attached to a dataset", which is a different and equally
legitimate answer to the same question.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "048"
down_revision: Union[str, None] = "047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("annotations",
                  sa.Column("kept_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("annotations", "kept_reason")
