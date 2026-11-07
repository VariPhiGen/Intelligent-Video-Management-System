"""Mirror a short clip of each published annotation, under its own S3 prefix.

`qa_sink_config.s3_video_prefix` is where the clips go. NULL or empty means
"do not publish video", which is what every appliance did before this column
existed, so nothing changes until an administrator fills the field in.

`qa_publish_outbox.video_key` records the object the relay actually wrote, for
the same reason `labelled_image_key` does (migration 060): `forget_images` has
to be able to name every object a row created, and a naming rule that changes
later must not strand the old ones in a public bucket. NULL means no clip was
stored for that row — either the prefix was off when it was queued, or the NVR
had no footage and the message went out without one.

Revision ID: 062
Revises: 061
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "062"
down_revision: Union[str, None] = "061"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "s3_video_prefix" not in _columns("qa_sink_config"):
        op.add_column("qa_sink_config",
                      sa.Column("s3_video_prefix", sa.Text, nullable=True))
    if "video_key" not in _columns("qa_publish_outbox"):
        op.add_column("qa_publish_outbox",
                      sa.Column("video_key", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("qa_publish_outbox", "video_key")
    op.drop_column("qa_sink_config", "s3_video_prefix")
