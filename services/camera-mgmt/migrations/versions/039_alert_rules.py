"""alert_rules: the rules engine table, and the FK migration 022 deferred.

Revision ID: 039
Revises: 038
Create Date: 2026-09-18

Migration 022 built `alerts` with a bare `rule_id uuid` and said the FK would
land "in 023 with the rules engine". 023 became STQC certification and the
engine sat unbuilt; 033-038 went to activity-catalogue, face-domain and search
defaults. This is that migration, arriving sixteen revisions late.

TWO DECISIONS WORTH THE INK.

`mode` defaults to 'log_only'. A rule that has never been reviewed cannot move
a barrier; it raises its alert and records what it WOULD have done. The
alternative — defaulting to armed and trusting the author — means the only way
to validate a rule is to let it swing a real gate.

`alerts.rule_id` is ON DELETE SET NULL, not CASCADE. `alerts.rule_name` was
denormalised in 022 precisely so a historical alert still reads correctly once
its rule is renamed or deleted. Deleting a rule must never delete evidence.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "039"
down_revision: Union[str, None] = "038"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "alert_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("mode", sa.String(12), nullable=False, server_default="log_only"),
        # Soft reference to analytics_activity_types.key, NOT a FK — same
        # reasoning as analytics_events.activity: the pipeline emits more
        # activity types than the catalogue currently offers, and a rule for a
        # type that is withdrawn must stay readable rather than vanish.
        sa.Column("activity", sa.String(64), nullable=False),
        sa.Column("scope_kind", sa.String(16), nullable=False),
        sa.Column("scope_value", sa.String(160), nullable=True),
        sa.Column("min_confidence", sa.Float(), nullable=True),
        sa.Column("conditions", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("cooldown_s", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("max_event_age_s", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("severity", sa.String(16), nullable=False, server_default="info"),
        sa.Column("actions", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("active_hours", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.CheckConstraint("mode IN ('log_only','armed')", name="ck_alert_rules_mode"),
        sa.CheckConstraint("scope_kind IN ('camera','zone','any')",
                           name="ck_alert_rules_scope_kind"),
        sa.CheckConstraint("cooldown_s >= 0", name="ck_alert_rules_cooldown"),
        sa.CheckConstraint("max_event_age_s > 0", name="ck_alert_rules_max_age"),
        # A zone- or camera-scoped rule with no value would match nothing and
        # look configured. 'any' is the only kind that may leave it NULL.
        sa.CheckConstraint(
            "(scope_kind = 'any' AND scope_value IS NULL) OR "
            "(scope_kind <> 'any' AND scope_value IS NOT NULL)",
            name="ck_alert_rules_scope_value",
        ),
    )
    # Case-insensitive uniqueness, the cameras.py pattern: the application
    # pre-checks for a friendly message, this index is the real guard.
    op.execute("CREATE UNIQUE INDEX ix_alert_rules_name_lower "
               "ON alert_rules (lower(name))")
    # The engine's hot lookup: enabled rules for one activity.
    op.execute("CREATE INDEX ix_alert_rules_activity ON alert_rules (activity) "
               "WHERE enabled")

    # The FK 022 deferred. SET NULL, never CASCADE — see the module docstring.
    op.create_foreign_key("fk_alerts_rule", "alerts", "alert_rules",
                          ["rule_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.drop_constraint("fk_alerts_rule", "alerts", type_="foreignkey")
    op.drop_index("ix_alert_rules_activity", table_name="alert_rules")
    op.drop_index("ix_alert_rules_name_lower", table_name="alert_rules")
    op.drop_table("alert_rules")
