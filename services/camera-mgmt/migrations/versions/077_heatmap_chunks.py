"""heatmap chunks: 5-minute heat increments as numpy arrays in the object store

WHY ARRAYS AND NOT PICTURES. A rendered picture is a session's total at one
moment and cannot be cut or combined. The Heatmap page now asks for any range
("from 09:00 Monday to 18:00 Tuesday"), which needs the HEAT itself, in pieces
small enough to add up: one float32 array per camera per 5 minutes, holding the
heat added in those 5 minutes. A range is the sum of its blocks, rendered once.

The array lives in MinIO (heatmaps/{slug}/{date}/chunks/{HHMMSS}.npz); this row
is the index that makes "which blocks are in this range" one range scan.

Revision ID: 077
Revises: 076
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "077"
down_revision: Union[str, None] = "076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "heatmap_chunks",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("camera_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("frame_width", sa.Integer, nullable=False),
        sa.Column("frame_height", sa.Integer, nullable=False),
        sa.Column("scale", sa.Integer, nullable=False, server_default="4"),
        sa.Column("points", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("peak", sa.Float, nullable=False, server_default="0"),
        sa.Column("params", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("object_key", sa.Text, nullable=False),
        sa.Column("background_key", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    # The range read, and the idempotent ingest (a retried block upserts).
    op.create_index("ux_heatmap_chunks_camera_start", "heatmap_chunks",
                    ["camera_id", "period_start"], unique=True)
    op.create_index("ix_heatmap_chunks_expires_at", "heatmap_chunks", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_heatmap_chunks_expires_at", table_name="heatmap_chunks")
    op.drop_index("ux_heatmap_chunks_camera_start", table_name="heatmap_chunks")
    op.drop_table("heatmap_chunks")
