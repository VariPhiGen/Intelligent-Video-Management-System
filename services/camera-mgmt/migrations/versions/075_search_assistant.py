"""Assistant readiness and administrator-owned activity aliases.

Revision ID: 075
Revises: 074
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '075'
down_revision = '074'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('search_planner_settings', sa.Column('assistant_tested_fingerprint', sa.String(64), nullable=True))
    for name in ('search_aliases', 'suggested_aliases'):
        op.add_column('analytics_activity_types', sa.Column(name, postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")))


def downgrade():
    for name in ('suggested_aliases', 'search_aliases'):
        op.drop_column('analytics_activity_types', name)
    op.drop_column('search_planner_settings', 'assistant_tested_fingerprint')
