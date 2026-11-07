"""Event integrations: the playback-link base URL is set in the admin UI.

It was only EVENTS_PUBLIC_BASE_URL, which meant editing .env and recreating
the API to turn playback links on. The saved value wins; the environment
variable remains the fallback for appliances provisioned that way.
"""
from alembic import op
import sqlalchemy as sa

revision = '067'
down_revision = '066'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('event_integration_settings', sa.Column('public_base_url', sa.Text, nullable=True))


def downgrade():
    op.drop_column('event_integration_settings', 'public_base_url')
