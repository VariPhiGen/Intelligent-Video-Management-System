"""Optional event destinations, subscriptions, durable delivery and feed."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY
revision = '065'
down_revision = '064'
branch_labels = depends_on = None

def ts(name, nullable=False):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable,
                     server_default=None if nullable else sa.func.now())

def upgrade():
    op.create_table('event_destinations',
        sa.Column('id', UUID, primary_key=True), sa.Column('name', sa.String(120), nullable=False, unique=True),
        sa.Column('kind', sa.String(16), nullable=False), sa.Column('config', JSONB, nullable=False, server_default='{}'),
        sa.Column('secret_enc', sa.Text), sa.Column('enabled', sa.Boolean, nullable=False, server_default='false'),
        sa.Column('media', sa.Boolean, nullable=False, server_default='false'),
        sa.Column('allow_insecure', sa.Boolean, nullable=False, server_default='false'),
        sa.Column('rate_per_min', sa.Integer, nullable=False, server_default='60'), sa.Column('max_age_s', sa.Integer),
        ts('next_send_at',True), ts('last_success_at',True), ts('last_error_at',True), sa.Column('last_error',sa.Text),
        ts('created_at'),ts('updated_at'),sa.CheckConstraint("kind IN ('webhook','telegram','slack','kafka')"))
    op.create_table('event_subscriptions', sa.Column('id',UUID,primary_key=True),
        sa.Column('destination_id',UUID,sa.ForeignKey('event_destinations.id',ondelete='CASCADE'),nullable=False),
        sa.Column('name',sa.String(120),nullable=False), sa.Column('types',ARRAY(sa.String),nullable=False),
        sa.Column('filters',JSONB,nullable=False,server_default='{}'), sa.Column('cooldown_s',sa.Integer),
        sa.Column('enabled',sa.Boolean,nullable=False,server_default='true'),ts('created_at'),ts('updated_at'))
    op.create_table('event_outbox',sa.Column('seq',sa.BigInteger,primary_key=True,autoincrement=True),
        sa.Column('message_id',UUID,unique=True,nullable=False),sa.Column('type',sa.String(40),nullable=False),
        sa.Column('subject_id',sa.String(80),nullable=False),sa.Column('camera_id',UUID),
        sa.Column('envelope',JSONB,nullable=False),sa.Column('routes',JSONB,nullable=False,server_default='[]'),
        ts('occurred_at'),ts('created_at'),ts('routed_at',True))
    op.create_index('ix_event_unrouted','event_outbox',['seq'],postgresql_where=sa.text('routed_at IS NULL'))
    op.create_table('event_deliveries',sa.Column('id',sa.BigInteger,primary_key=True,autoincrement=True),
        sa.Column('outbox_seq',sa.BigInteger,sa.ForeignKey('event_outbox.seq',ondelete='CASCADE'),nullable=False),
        sa.Column('destination_id',UUID,sa.ForeignKey('event_destinations.id',ondelete='CASCADE'),nullable=False),
        sa.Column('destination',JSONB,nullable=False),sa.Column('subscription_ids',ARRAY(UUID),nullable=False),
        sa.Column('state',sa.String(12),nullable=False,server_default='pending'),
        sa.Column('attempts',sa.Integer,nullable=False,server_default='0'),ts('next_attempt_at'),
        sa.Column('lease_owner',sa.String(64)),ts('lease_until',True),ts('deadline_at'),
        sa.Column('last_error',sa.Text),sa.Column('provider_ref',sa.String(120)),sa.Column('media',sa.String(16)),
        ts('created_at'),ts('sent_at',True),sa.UniqueConstraint('outbox_seq','destination_id'),
        sa.CheckConstraint("state IN ('pending','sending','sent','retrying','failed','expired','suppressed')"))
    op.create_index('ix_event_destination_queue','event_deliveries',['destination_id','outbox_seq'],postgresql_where=sa.text("state IN ('pending','retrying','sending')"))
    op.create_index('ix_event_due','event_deliveries',['state','next_attempt_at'])
    op.create_table('event_cooldowns',sa.Column('key',sa.Text,primary_key=True),ts('until'))
    op.create_table('event_integration_settings',sa.Column('id',sa.Integer,primary_key=True),
        sa.Column('enabled',sa.Boolean,nullable=False,server_default='true'),
        sa.Column('feed_enabled',sa.Boolean,nullable=False,server_default='false'),
        sa.Column('purged_through',sa.BigInteger,nullable=False,server_default='0'),sa.CheckConstraint('id = 1'))
    op.execute('INSERT INTO event_integration_settings(id) VALUES (1)')

def downgrade():
    for name in ('event_integration_settings','event_cooldowns','event_deliveries','event_outbox','event_subscriptions','event_destinations'):
        op.drop_table(name)
