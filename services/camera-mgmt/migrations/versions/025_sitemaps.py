"""Sitemaps (floor-plan images) for the Map tab.

Revision ID: 025
Revises: 024
Create Date: 2026-08-17

Image bytes live in Postgres (the API container has no writable volume) and
are served via GET /api/sitemaps/{id}/image. Camera placement stays in
camera_metadata JSONB — deleting a sitemap sweeps that key in the DELETE
handler, so no FK is needed here.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "025"
down_revision: Union[str, None] = "023"   # was "024"; that revision moved to the compliance extension
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sitemaps",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("content_type", sa.String(64), nullable=False),
        sa.Column("image", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uploaded_by", sa.String(255), nullable=True),
    )
    # Case-insensitive uniqueness: "Ground Floor" and "ground floor" collide.
    op.create_index(
        "ix_sitemaps_name_lower", "sitemaps", [sa.text("lower(name)")], unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_sitemaps_name_lower", table_name="sitemaps")
    op.drop_table("sitemaps")
