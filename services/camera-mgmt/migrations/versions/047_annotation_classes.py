"""Label classes, and annotations that name one.

Revision ID: 047
Revises: 046
Create Date: 2026-09-25

Two tables and one rule that shapes both.

`label_classes` is the fleet taxonomy an administrator owns. It is modelled on
`analytics_activity_types` (036) and inherits its most important property:
A CLASS IS RETIRED, NEVER DELETED. Annotations name a class by key, and those
rows outlive an administrator's change of mind about what the taxonomy should
contain. `active = false` removes it from the picker; the FK below makes
deletion impossible while anything references it, so the retirement path is the
only path.

`annotations` indexes what the NVR already writes to disk. The JPEG and its
JSON sidecar stay where they are: the sidecar carries every field needed to
rebuild a row, which is what makes the file-before-row ordering recoverable.
This table is the index, not the record of truth for the pixels.

camera_slug is deliberately NOT a foreign key to cameras.id, for the reason
migration 045 records for datasets: an annotation outlives the camera row it
was drawn on, and losing training material because someone decommissioned a
camera would be the wrong behaviour.

dataset_id is ON DELETE SET NULL, not CASCADE. A dataset is working material
and deletable on purpose (045); the annotation is the labelled fact and should
survive its container.

expires_at null means "no expiry" — the posture a dataset-attached annotation
takes, because it then has a named owner and a deletable parent. Everything
else is stamped at insert from ANNOTATION_TTL_MINUTES.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "047"
down_revision: Union[str, None] = "046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "label_classes",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("color", sa.String(9), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )

    op.create_table(
        "annotations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("camera_slug", sa.Text(), nullable=False),
        sa.Column("class_key", sa.String(64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bbox_x", sa.Float(), nullable=False),
        sa.Column("bbox_y", sa.Float(), nullable=False),
        sa.Column("bbox_w", sa.Float(), nullable=False),
        sa.Column("bbox_h", sa.Float(), nullable=False),
        sa.Column("frame_width", sa.Integer(), nullable=False),
        sa.Column("frame_height", sa.Integer(), nullable=False),
        sa.Column("image_path", sa.Text(), nullable=False),
        sa.Column("mask_path", sa.Text(), nullable=True),
        sa.Column("dataset_id", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["class_key"], ["label_classes.key"],
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.id"],
                                ondelete="SET NULL"),
        sa.CheckConstraint("bbox_w > 0 AND bbox_h > 0", name="ck_annotations_bbox"),
    )
    op.create_index("ix_annotations_camera_time", "annotations",
                    ["camera_slug", "captured_at"])
    op.create_index("ix_annotations_class", "annotations", ["class_key"])
    op.create_index("ix_annotations_dataset", "annotations", ["dataset_id"])
    op.create_index("ix_annotations_expires", "annotations", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_annotations_expires", table_name="annotations")
    op.drop_index("ix_annotations_dataset", table_name="annotations")
    op.drop_index("ix_annotations_class", table_name="annotations")
    op.drop_index("ix_annotations_camera_time", table_name="annotations")
    op.drop_table("annotations")
    op.drop_table("label_classes")
