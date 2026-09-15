"""Merge the discovery service's discovered_devices into cameras.

Revision ID: 004
Revises: 003
Create Date: 2026-07-06

One unified cameras table for the whole camera lifecycle:
  discovered → probing → verified → registered (or ignored / auth_failed / …)

Existing rows are all real registry cameras, so they get stage='registered'.
name/slug/rtsp_url become nullable (pre-registration rows don't have them);
a CHECK constraint keeps them mandatory for registered rows. `ip` is backfilled
from rtsp_url so network-scan dedupe recognises already-registered cameras.
Old rows in the separate rtsp_discovery database are NOT migrated here (this
migration runs against rtsp_relay only) — see scripts/import_discovery_db.py
for a one-shot import, or simply rescan.
"""
from __future__ import annotations

from typing import Sequence, Union
from urllib.parse import urlparse

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Relax registration-only NOT NULLs (unique indexes ignore NULLs) ──────
    op.alter_column("cameras", "name", existing_type=sa.String(255), nullable=True)
    op.alter_column("cameras", "slug", existing_type=sa.String(255), nullable=True)
    op.alter_column("cameras", "rtsp_url", existing_type=sa.Text(), nullable=True)

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    op.add_column(
        "cameras",
        sa.Column("stage", sa.String(32), nullable=False, server_default="registered"),
    )
    op.add_column(
        "cameras",
        sa.Column("recording", sa.Boolean(), nullable=False, server_default="true"),
    )

    # ── Discovery data ────────────────────────────────────────────────────────
    op.add_column("cameras", sa.Column("ip", sa.String(64), nullable=True))
    op.add_column("cameras", sa.Column("mac", sa.String(32), nullable=True))
    op.add_column("cameras", sa.Column("onvif_port", sa.Integer(), nullable=True))
    op.add_column("cameras", sa.Column("rtsp_port", sa.Integer(), nullable=True))
    op.add_column("cameras", sa.Column("vendor", sa.String(128), nullable=True))
    op.add_column("cameras", sa.Column("model", sa.String(128), nullable=True))
    op.add_column("cameras", sa.Column("firmware", sa.String(128), nullable=True))
    op.add_column(
        "cameras",
        sa.Column("rtsp_candidates", JSONB, nullable=False, server_default="[]"),
    )
    op.add_column(
        "cameras",
        sa.Column("open_ports", JSONB, nullable=False, server_default="[]"),
    )
    op.add_column("cameras", sa.Column("enc_username", sa.Text(), nullable=True))
    op.add_column("cameras", sa.Column("enc_password", sa.Text(), nullable=True))
    op.add_column("cameras", sa.Column("discovery_error", sa.Text(), nullable=True))
    op.add_column(
        "cameras",
        sa.Column("last_scanned_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index("ix_cameras_stage", "cameras", ["stage"])
    # Non-unique on purpose: multi-channel encoders share one IP, manual rows may lack it.
    op.create_index("ix_cameras_ip", "cameras", ["ip"])

    op.create_check_constraint(
        "ck_cameras_registered_complete",
        "cameras",
        "stage <> 'registered' OR (name IS NOT NULL AND slug IS NOT NULL AND rtsp_url IS NOT NULL)",
    )

    # ── Backfill ip from rtsp_url (scan dedupe key) ───────────────────────────
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, rtsp_url FROM cameras WHERE rtsp_url IS NOT NULL")
    ).fetchall()
    for row_id, rtsp_url in rows:
        try:
            host = urlparse(rtsp_url).hostname
        except ValueError:
            host = None
        if host:
            conn.execute(
                sa.text("UPDATE cameras SET ip = :ip WHERE id = :id"),
                {"ip": host[:64], "id": row_id},
            )


def downgrade() -> None:
    op.drop_constraint("ck_cameras_registered_complete", "cameras")
    op.drop_index("ix_cameras_ip", table_name="cameras")
    op.drop_index("ix_cameras_stage", table_name="cameras")
    for col in (
        "last_scanned_at", "discovery_error", "enc_password", "enc_username",
        "open_ports", "rtsp_candidates", "firmware", "model", "vendor",
        "rtsp_port", "onvif_port", "mac", "ip", "recording", "stage",
    ):
        op.drop_column("cameras", col)
    # Staging rows (NULL name/slug/rtsp_url) cannot survive the NOT NULLs.
    op.execute("DELETE FROM cameras WHERE name IS NULL OR slug IS NULL OR rtsp_url IS NULL")
    op.alter_column("cameras", "rtsp_url", existing_type=sa.Text(), nullable=False)
    op.alter_column("cameras", "slug", existing_type=sa.String(255), nullable=False)
    op.alter_column("cameras", "name", existing_type=sa.String(255), nullable=False)
