"""Event integrations: email and SFTP destinations, and durable event media.

Email goes through ONE site mail server (event_integration_settings.smtp), the
way Axis Camera Station and Nx configure it; each email destination only picks
recipients. SFTP keeps its connection in the destination like the others.

event_media holds the snapshot taken for an event, once. Email retries and SFTP
exports reuse it, so a retry tomorrow can never attach tomorrow's live frame to
yesterday's event. It cascades with its outbox row, so the existing retention
sweep is also its cleanup.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '068'
down_revision = '067'
branch_labels = None
depends_on = None

_OLD = "kind IN ('webhook','telegram','slack','kafka')"
_NEW = "kind IN ('webhook','telegram','slack','kafka','email','sftp')"


def upgrade():
    op.drop_constraint('event_destinations_kind_check', 'event_destinations', type_='check')
    op.create_check_constraint('event_destinations_kind_check', 'event_destinations', _NEW)
    op.add_column('event_integration_settings', sa.Column('smtp', postgresql.JSONB, nullable=True))
    op.add_column('event_integration_settings', sa.Column('smtp_secret_enc', sa.Text, nullable=True))
    op.create_table(
        'event_media',
        sa.Column('outbox_seq', sa.BigInteger,
                  sa.ForeignKey('event_outbox.seq', ondelete='CASCADE'), primary_key=True),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('source', sa.String(24), nullable=True),
        sa.Column('caption', sa.Text, nullable=True),
        sa.Column('captured_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sha256', sa.String(64), nullable=True),
        sa.Column('size', sa.Integer, nullable=False, server_default='0'),
        sa.Column('content', sa.LargeBinary, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade():
    op.drop_table('event_media')
    op.drop_column('event_integration_settings', 'smtp_secret_enc')
    op.drop_column('event_integration_settings', 'smtp')
    op.execute("DELETE FROM event_destinations WHERE kind IN ('email','sftp')")
    op.drop_constraint('event_destinations_kind_check', 'event_destinations', type_='check')
    op.create_check_constraint('event_destinations_kind_check', 'event_destinations', _OLD)
