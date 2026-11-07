"""Record the labelled copy's object key alongside the original's.

A published annotation now puts TWO objects in the bucket: the frame as
captured, and a copy with the operator's box burned onto it. The second is
what `org_img_local` points at, so anything downstream that reads a flat image
gets one that shows what was annotated.

WHY A COLUMN RATHER THAN DERIVING IT. The labelled key is `<id>_labelled.jpg`
next to `<id>.jpg`, so it looks derivable — and deriving it is exactly the
mistake. `forget_images` is what deletes these when an annotation expires or a
subject erasure comes in, and it has to be able to name every object this row
created. A naming rule that changes later would leave the old copies
undeletable, publicly readable, with nothing on the appliance recording that
they exist. The row stores what it actually wrote.

Nullable: rows published before this migration have one object, and null is
the honest record of that rather than a guess at a key that was never used.

RENUMBERED 055 -> 060 (2026-10-15): written on the `manual` numbering, where the
QA sink was 053/054; on `field` 055 is branding and the QA sink is 058/059, so
two "055"s broke `alembic upgrade`. Column-guarded for a `manual` database that
already ran it as 055 (backend/migrate.py re-stamps those to 052 and replays).

Revision ID: 060
Revises: 059
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "060"
down_revision: Union[str, None] = "059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if any(c["name"] == "labelled_image_key"
           for c in sa.inspect(op.get_bind()).get_columns("qa_publish_outbox")):
        return
    op.add_column("qa_publish_outbox",
                  sa.Column("labelled_image_key", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("qa_publish_outbox", "labelled_image_key")
