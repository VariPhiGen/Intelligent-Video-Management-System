"""Immutable, hash-chained audit log (DPDP Rule 6 / STQC Level-2 control).

Revision ID: 019
Revises: 018
Create Date: 2026-08-04

One append-only row per auditable action (login, export, config change,
retention override, role grant, …). Each row carries ``entry_hash =
sha256(prev_hash || canonical(payload))`` so any later edit to a row — or a
deletion that breaks the chain — is detectable on read. A trigger blocks UPDATE
and DELETE outright: the log is write-once at the DB layer, not just by
convention. Retention is unbounded for now (we keep everything); a tail-purge
that preserves the chain-from-a-checkpoint is a later concern.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "019"
down_revision: Union[str, None] = "018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        # BIGSERIAL: monotonic append order, and the spine of the hash chain.
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        # Set in Python (UTC) so the value that was hashed equals the value stored.
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False),  # user|service|system|unknown
        sa.Column("actor", sa.String(255), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target", sa.String(255), nullable=True),
        sa.Column("detail", JSONB, nullable=False, server_default="{}"),
        sa.Column("source_ip", sa.String(64), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=False, server_default="success"),
        sa.Column("prev_hash", sa.String(64), nullable=True),   # null at genesis
        sa.Column("entry_hash", sa.String(64), nullable=False),
    )
    op.create_index("ix_audit_log_ts", "audit_log", ["ts"])
    op.create_index("ix_audit_log_actor", "audit_log", ["actor"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])

    # Write-once enforcement: refuse UPDATE and DELETE at the database, so a
    # compromised app account (or a careless migration) cannot silently rewrite
    # history. Appends via INSERT are unaffected.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_immutable()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only: % is not permitted', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_no_update_delete
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log;")
    op.execute("DROP FUNCTION IF EXISTS audit_log_immutable();")
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_actor", table_name="audit_log")
    op.drop_index("ix_audit_log_ts", table_name="audit_log")
    op.drop_table("audit_log")
