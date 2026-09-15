"""Unique constraint on cameras.rtsp_url

Revision ID: 002
Revises: 001
Create Date: 2026-05-08

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_cameras_rtsp_url", "cameras", ["rtsp_url"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_cameras_rtsp_url", table_name="cameras")
