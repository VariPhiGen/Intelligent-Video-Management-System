"""Event integrations: a gap-safe feed cursor and machine-sized default rates.

WHY feed_seq EXISTS. `event_outbox.seq` is assigned at INSERT, but rows commit
in whatever order their transactions finish. A feed reader at cursor 10 could
miss row 9 committing a moment later. 065 prevented that with one global
advisory lock taken by every enqueue — which serialised all ingest and, worse,
deadlocked against the camera health check (health holds the camera row and
wants the lock; ingest holds the lock and needs the camera row for its FK).

The router is a single serialised writer that commits in order, so it now
stamps `feed_seq` as it routes. The feed pages on that, and enqueue takes no
global lock.

Existing rows get feed_seq = seq: everything already committed is safe to
expose in seq order.
"""
from alembic import op
import sqlalchemy as sa

revision = '066'
down_revision = '065'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('CREATE SEQUENCE event_outbox_feed_seq')
    op.add_column('event_outbox', sa.Column('feed_seq', sa.BigInteger, nullable=True))
    op.execute('UPDATE event_outbox SET feed_seq = seq WHERE routed_at IS NOT NULL')
    op.execute("SELECT setval('event_outbox_feed_seq', GREATEST((SELECT COALESCE(MAX(seq), 0) FROM event_outbox), 1))")
    op.create_index('ux_event_outbox_feed_seq', 'event_outbox', ['feed_seq'], unique=True,
                    postgresql_where=sa.text('feed_seq IS NOT NULL'))
    # 065 defaulted every kind to 60/min, which capped a Kafka or LAN webhook
    # feed at one event per second. 60 on a machine kind is that default, not
    # a choice anyone made.
    op.execute("UPDATE event_destinations SET rate_per_min = 6000 "
               "WHERE kind IN ('webhook','kafka') AND rate_per_min = 60")
    op.execute("UPDATE event_destinations SET rate_per_min = 20 "
               "WHERE kind = 'telegram' AND rate_per_min = 60")


def downgrade():
    op.drop_index('ux_event_outbox_feed_seq', table_name='event_outbox')
    op.drop_column('event_outbox', 'feed_seq')
    op.execute('DROP SEQUENCE event_outbox_feed_seq')
