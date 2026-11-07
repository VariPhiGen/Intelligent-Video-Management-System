"""heatmap background: ONE camera frame per camera, overwritten, not an archive

The heatmap of any range is drawn over a camera frame. Until now a frame was
stored in MinIO with a heat block about every 30 minutes and kept for the
camera's whole retention -- hundreds of pictures of the scene that only ever
served as wallpaper, and only the newest one near a range was ever read.

Now there is one row per camera holding the newest frame. Analytics sends a
frame with every 5-minute block; each one REPLACES the last, so the previous
frame is gone the moment the next arrives. Today's heatmap and every past range
are drawn over the same, current picture of the scene.

`expires_at` bounds a camera that stops sending (heatmap switched off for good,
schedule removed): the retention sweep deletes a frame nobody has refreshed for
the camera's footage retention, so a picture of people never outlives it.

The old `heatmap_chunks.background_key` column stays (rows written before this
still name objects the retention sweep must delete), but nothing writes it.

Revision ID: 078
Revises: 077
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "078"
down_revision: Union[str, None] = "077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "heatmap_backgrounds",
        sa.Column("camera_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("cameras.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("image", sa.LargeBinary, nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("heatmap_backgrounds")
