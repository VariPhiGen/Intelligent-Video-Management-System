"""The QA sink gets its own S3 credentials, separate from the dataset mirror's.

WHY THESE CANNOT SHARE A KEY. `AWS_*` in the environment belongs to the Data
Collection mirror, whose bucket holds training data and is written by a key
provisioned for exactly that. The QA sink writes somewhere else entirely — a
prefix that has to be world-readable so a reviewer's browser can load it — and
the two destinations are owned by different people in practice. Forcing them
onto one key means either the training key gains write access to a public
prefix, or the public bucket's key gains access to the training data. Both are
worse than a second field on a form.

NULL MEANS "USE THE ENVIRONMENT", and that is the whole compatibility story:
an appliance that already works keeps working, because a blank key here falls
through to exactly the credentials `s3_upload` has always used. Nobody has to
fill these in to keep what they have.

The secret is Fernet-encrypted at rest by the same crypto.py that holds camera
passwords and the broker's SASL password. A region is here too because the
bucket may well not be in the same one as the dataset mirror, and the virtual-
hosted URL that reviewers load has the region baked into the hostname.

Renumbered 054 -> 059 with 058 (see 058_qa_sink.py); guarded per column for the
same replay.

Revision ID: 059
Revises: 058
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "059"
down_revision: Union[str, None] = "058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("qa_sink_config")}
    for column in ("aws_access_key_id", "aws_secret_access_key_enc", "aws_region"):
        if column not in have:
            op.add_column("qa_sink_config", sa.Column(column, sa.Text, nullable=True))


def downgrade() -> None:
    for column in ("aws_region", "aws_secret_access_key_enc",
                   "aws_access_key_id"):
        op.drop_column("qa_sink_config", column)
