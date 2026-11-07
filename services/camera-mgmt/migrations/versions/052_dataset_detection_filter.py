"""datasets: optional detection filter (keep only frames containing a class)

A dataset may now name an uploaded detection model (held by the analytics
service) and the classes to look for. Collection then scans the window every
`scan_interval_seconds`, runs the model on each frame and keeps only frames in
which a selected class was detected; `target_frames` becomes the cap on frames
KEPT rather than the number sampled. `frames_scanned` records how many were
examined, so "scanned 900, kept 40" is visible.

All columns are nullable / defaulted: existing datasets are plain evenly-spaced
sampling, exactly as before.

Revision ID: 052
Revises: 051
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "052"
down_revision: Union[str, None] = "051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("datasets", sa.Column("model_id", sa.String(64), nullable=True))
    op.add_column("datasets", sa.Column("model_name", sa.Text, nullable=True))
    op.add_column("datasets", sa.Column(
        "target_classes", postgresql.JSONB, nullable=False, server_default="[]"))
    op.add_column("datasets", sa.Column("min_confidence", sa.Float, nullable=True))
    op.add_column("datasets", sa.Column(
        "scan_interval_seconds", sa.Float, nullable=True))
    op.add_column("datasets", sa.Column(
        "frames_scanned", sa.Integer, nullable=False, server_default="0"))
    op.create_check_constraint(
        "ck_datasets_scan_interval", "datasets",
        "scan_interval_seconds IS NULL OR scan_interval_seconds >= 0.5")
    op.create_check_constraint(
        "ck_datasets_min_confidence", "datasets",
        "min_confidence IS NULL OR (min_confidence > 0 AND min_confidence < 1)")


def downgrade() -> None:
    op.drop_constraint("ck_datasets_min_confidence", "datasets", type_="check")
    op.drop_constraint("ck_datasets_scan_interval", "datasets", type_="check")
    for c in ("frames_scanned", "scan_interval_seconds", "min_confidence",
              "target_classes", "model_name", "model_id"):
        op.drop_column("datasets", c)
