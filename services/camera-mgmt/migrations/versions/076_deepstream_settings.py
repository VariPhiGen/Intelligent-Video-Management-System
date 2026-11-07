"""DeepStream settings — the pipeline control API's address, set in the UI (one row, id=1).

DEEPSTREAM_API_URL in the environment was the only way to point the VMS at the
pipeline, so a pipeline on another host (or port) meant editing .env and
recreating the api container. Administration → DeepStream now stores it here;
NULL / no row keeps the environment value (services/deepstream_client.load_url).

Revision ID: 076
Revises: 075
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "076"
down_revision: Union[str, None] = "075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "deepstream_settings" not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table(
            "deepstream_settings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("api_url", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_by", sa.String(255), nullable=True),
        )


def downgrade() -> None:
    op.drop_table("deepstream_settings")
