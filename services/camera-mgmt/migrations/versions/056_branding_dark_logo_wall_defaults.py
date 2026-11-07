"""Branding: dark-theme logo + the admin's default video-wall settings.

RENUMBERED on the field -> ai merge (was 052); see backend/migrate.py.

Revision ID: 056
Revises: 055
Create Date: 2026-10-04

Found in use: an admin set up the wall (cameras, padding, logo size) and an operator on another
browser saw none of it — every wall setting lived only in the browser that made it. The admin now
saves a DEFAULT here that every browser starts from; browsers may still override single fields
locally (a TV whose overlay needs more top padding). Plus a second logo for the dark theme, since a
dark logo disappears on the dark UI and vice versa.

All columns nullable: NULL = "not set", existing installs keep exactly what they had.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


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

revision: str = "056"
down_revision: Union[str, None] = "055"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not _has_column("branding", "logo_dark"):
        op.add_column("branding", sa.Column("logo_dark", sa.LargeBinary(), nullable=True))
    if not _has_column("branding", "logo_dark_content_type"):
        op.add_column("branding", sa.Column("logo_dark_content_type", sa.String(64), nullable=True))
    if not _has_column("branding", "wall_defaults"):
        op.add_column("branding", sa.Column("wall_defaults", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("branding", "wall_defaults")
    op.drop_column("branding", "logo_dark_content_type")
    op.drop_column("branding", "logo_dark")
