"""Provider settings and encrypted credentials for natural-language search.

Revision ID: 063
Revises: 062
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '063'
down_revision = '062'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('search_planner_settings',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('config', JSONB, nullable=False),
        sa.Column('credentials', JSONB, nullable=False),
        sa.Column('tested_fingerprint', sa.String(64), nullable=True),
        sa.CheckConstraint('id = 1', name='ck_search_planner_singleton'))


def downgrade():
    op.drop_table('search_planner_settings')
