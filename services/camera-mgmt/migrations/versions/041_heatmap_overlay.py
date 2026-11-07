"""heatmap sessions, artifacts and the runtime switch.

Revision ID: 041
Revises: 040
Create Date: 2026-09-20

THREE TABLES, BECAUSE THEY ANSWER THREE DIFFERENT QUESTIONS.

`heatmap_sessions` — what one accumulation was: when it started, when and why
it stopped, and the settings the heat was measured with. Its id is minted by
the ANALYTICS service, like `analytics_events.id`: analytics is the only thing
that knows when heat actually began accumulating, and a client-minted id makes
a retried upload update one row instead of writing a second.

`heatmap_artifacts` — the rendered pictures. Bytes live in Postgres, as
`sitemaps.image` does and for the same reason: the api container has no
writable volume, and a heatmap JPEG is image-sized (~200-400 KB), not
video-sized. Following the DeepStream reference, which uploads exactly ONE
image per activity window, the steady state is a rolling "latest" plus the
final picture of each session — two rows per camera per day, well under a
megabyte. `object_key` is the unused seam for an object store: fill it, leave
`image` null, and nothing above this table changes.

`heatmap_camera_state` — the operator's ON/OFF switch, explicitly, so it is
never confused with the AI Config schedule. The schedule says when the heatmap
is ALLOWED to run and lives in `cameras.analytics_config`; this says whether
the operator wants it running now. Analytics holds the same flag in memory
only, so this row is what the reconcile loop re-asserts after a restart.

RETENTION is `analytics_events`': `expires_at` stamped at insert from the
owning camera's footage retention, with an index so the sweep is a cheap
`DELETE WHERE expires_at < now()`. A heatmap of a period whose footage is gone
has nothing left to check it against.

The unique index on (session_id, kind, captured_at) is what makes the ingest
idempotent: the sink retries on any 5xx, and a retry must not leave two copies
of the same minute's picture.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "041"
down_revision: Union[str, None] = "040"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "heatmap_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("camera_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False),
        sa.Column("activity", sa.String(64), nullable=False,
                  server_default="Heatmap_overlay"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("close_reason", sa.String(64), nullable=True),
        sa.Column("params", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("points", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("frames", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('active','frozen','closed')",
                           name="ck_heatmap_sessions_status"),
    )
    op.execute("CREATE INDEX ix_heatmap_sessions_camera_time "
               "ON heatmap_sessions (camera_id, started_at DESC)")
    # The page asks "what is this camera doing now", which is the one session
    # per camera that has not been closed.
    op.execute("CREATE INDEX ix_heatmap_sessions_open ON heatmap_sessions (camera_id) "
               "WHERE status <> 'closed'")

    op.create_table(
        "heatmap_artifacts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("heatmap_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("camera_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="snapshot"),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("height", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("content_type", sa.String(64), nullable=False,
                  server_default="image/jpeg"),
        sa.Column("image", sa.LargeBinary(), nullable=True),
        sa.Column("object_key", sa.Text(), nullable=True),
        sa.Column("points", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("peak", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('snapshot','final')", name="ck_heatmap_artifacts_kind"),
    )
    op.execute("CREATE INDEX ix_heatmap_artifacts_camera_time "
               "ON heatmap_artifacts (camera_id, captured_at DESC)")
    op.execute("CREATE INDEX ix_heatmap_artifacts_session_time "
               "ON heatmap_artifacts (session_id, captured_at DESC)")
    op.create_index("ix_heatmap_artifacts_expires_at", "heatmap_artifacts", ["expires_at"])
    op.create_index("ux_heatmap_artifacts_capture", "heatmap_artifacts",
                    ["session_id", "kind", "captured_at"], unique=True)

    op.create_table(
        "heatmap_camera_state",
        sa.Column("camera_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("cameras.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("runtime_enabled", sa.Boolean(), nullable=False,
                  server_default=sa.text("true")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_by", sa.String(120), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("heatmap_camera_state")
    op.drop_index("ux_heatmap_artifacts_capture", table_name="heatmap_artifacts")
    op.drop_index("ix_heatmap_artifacts_expires_at", table_name="heatmap_artifacts")
    op.execute("DROP INDEX IF EXISTS ix_heatmap_artifacts_session_time")
    op.execute("DROP INDEX IF EXISTS ix_heatmap_artifacts_camera_time")
    op.drop_table("heatmap_artifacts")
    op.execute("DROP INDEX IF EXISTS ix_heatmap_sessions_open")
    op.execute("DROP INDEX IF EXISTS ix_heatmap_sessions_camera_time")
    op.drop_table("heatmap_sessions")
