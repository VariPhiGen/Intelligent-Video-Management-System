"""Per-role permission policies (the editable Roles & permissions matrix).

Revision ID: 009
Revises: 008
Create Date: 2026-07-13

Identity/roles stay in Keycloak; this table stores WHAT each role may do:
{"live_view": bool, "playback_search": bool, "export_reports": bool,
 "motion_ack": bool}. Only operator/dpo rows exist — admin always has full
access and is never stored. Absent rows/keys fall back to code defaults that
mirror pre-policy behavior (services/policy.py).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "role_policies",
        sa.Column("role", sa.String(32), primary_key=True),
        sa.Column("policy", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("role_policies")
