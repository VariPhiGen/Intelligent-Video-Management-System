"""heatmap chunks: their size, so a per-camera storage cap can be enforced

The Heatmap overlay activity now carries two operator-set caps, `retention_days`
and `storage_mb`, and the retention sweep deletes a camera's OLDEST heat blocks
past either one. The storage cap needs each block's size without asking MinIO
for every object on every sweep, so ingest records it here. Rows written before
this have NULL and are measured once by the sweep (one HEAD each).

The days cap replaces `expires_at` as the deadline for heat blocks, because a
deadline stamped at ingest cannot follow an operator who raises the cap later.
`expires_at` is still written, as the cap in force when the block arrived.

Revision ID: 079
Revises: 078
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "079"
down_revision: Union[str, None] = "078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("heatmap_chunks", sa.Column("size_bytes", sa.BigInteger, nullable=True))
    # The sweep walks one camera's blocks oldest first.
    op.create_index("ix_heatmap_chunks_camera_end", "heatmap_chunks",
                    ["camera_id", "period_end"])


def downgrade() -> None:
    op.drop_index("ix_heatmap_chunks_camera_end", table_name="heatmap_chunks")
    op.drop_column("heatmap_chunks", "size_bytes")
