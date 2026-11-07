"""Branding: the client's logo and the Video Wall title, set by an admin.

RENUMBERED on the field -> ai merge (was 051); see backend/migrate.py.

Revision ID: 055
Revises: 054
Create Date: 2026-10-05

One row (id = 1), created on first save. The logo is stored as bytes in Postgres
like sitemap images (the API container has no writable volume) and is optional,
as is the title — an installation that never sets either keeps the defaults
("Video Wall", no logo).

NUMBERING: 051 follows 050 on the `field` line; 037-049 stay reserved as
headroom for the `ai` branch (see 050's docstring).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# Idempotent on purpose (merge of `field` into the ai chain, 2026-10-07): appliances that ran the
# old `field` numbering already HAVE what this migration creates. backend/migrate.py re-stamps them onto
# the merged chain at 032 and replays forward, so every step that can meet an existing object checks first.
def _insp():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _insp().has_table(name)


def _has_column(table: str, column: str) -> bool:
    return _has_table(table) and any(c["name"] == column for c in _insp().get_columns(table))


def _has_index(table: str, index: str) -> bool:
    return _has_table(table) and any(i["name"] == index for i in _insp().get_indexes(table))

revision: str = "055"
down_revision: Union[str, None] = "054"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not _has_table("branding"):
        op.create_table(
            "branding",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("wall_title", sa.String(80), nullable=True),
            sa.Column("logo", sa.LargeBinary(), nullable=True),
            sa.Column("logo_content_type", sa.String(64), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_by", sa.String(255), nullable=True),
        )


def downgrade() -> None:
    op.drop_table("branding")
