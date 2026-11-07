"""integration credentials: the Home Assistant URL and token, set from the UI

Until now the HA driver read HA_URL / HA_TOKEN from the environment, which
means a token in a plaintext file and an api restart to change it. This table
holds it instead, set by an admin over /api/ha/connection:

  name        which integration ('homeassistant'); the primary key, so there
              is at most one of each and "which one is live" never needs asking
  url         where it is
  secret_enc  the token as Fernet ciphertext (backend/crypto.py, the same path
              camera credentials use). There is no plaintext column.

A row here WINS over the environment; with no row the environment still works,
so an appliance configured through .env keeps working across this migration.

Revision ID: 081
Revises: 080
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "081"
down_revision: Union[str, None] = "080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integration_credentials",
        sa.Column("name", sa.String(40), primary_key=True),
        sa.Column("url", sa.String(255), nullable=False),
        sa.Column("secret_enc", sa.Text, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("integration_credentials")
