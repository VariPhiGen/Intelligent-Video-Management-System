"""Annotation webhook delivery and immutable queued destinations."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '064'
down_revision = '063'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('qa_sink_config', sa.Column('delivery_method', sa.String(16), nullable=False, server_default='kafka'))
    op.add_column('qa_sink_config', sa.Column('message_format', sa.String(32), nullable=False, server_default='review_legacy'))
    op.add_column('qa_sink_config', sa.Column('webhook_url', sa.Text(), nullable=True))
    op.add_column('qa_sink_config', sa.Column('webhook_token_enc', sa.Text(), nullable=True))
    op.add_column('qa_publish_outbox', sa.Column('destination', JSONB, nullable=True))
    # Credentials already encrypted in config remain encrypted in the snapshot.
    op.execute("""UPDATE qa_publish_outbox SET destination = (
        SELECT to_jsonb(c) - 'id' - 'updated_at' - 'updated_by'
        FROM qa_sink_config c WHERE id = 1)""")


def downgrade():
    op.drop_column('qa_publish_outbox', 'destination')
    for column in ('webhook_token_enc', 'webhook_url', 'message_format', 'delivery_method'):
        op.drop_column('qa_sink_config', column)
