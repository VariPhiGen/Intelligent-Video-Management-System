"""Publish operator-drawn annotations to an external QA review queue over Kafka.

TWO TABLES, AND NEITHER IS OPTIONAL TO THE DESIGN.

`qa_sink_config` is a SINGLETON (`id` pinned to 1 by a check constraint). The
destination is one external broker per appliance, not a per-camera or
per-dataset choice, and a singleton row is the honest shape for that — a table
that can hold two rows invites the question "which one is live?", which has no
good answer. It is a table rather than environment variables because the broker,
the topic and the class map are things an operator changes from Administration
without a redeploy, exactly like `label_classes` (migration 047).

`qa_publish_outbox` is a transactional outbox. The annotation row and its
outbox row are written in ONE transaction; a relay loop drains the outbox
afterwards. Publishing inline from the request handler was the alternative and
it is wrong twice over: a broker that is slow makes the operator wait for a save
that has nothing to do with the broker, and a broker that is down loses the
message with the request that carried it. This appliance is on-premises behind
somebody else's uplink — "the network was out for ten minutes" is a Tuesday, not
an incident, and the outbox is what makes it a non-event.

`annotation_id` is the PRIMARY KEY, not a plain column. One annotation gets at
most one outbox row, so a double-publish is impossible by construction rather
than by a uniqueness check somebody remembers to write. ON DELETE CASCADE is
load-bearing for privacy, not tidiness: `annotation_store.purge_expired` and
`erase_range` delete annotations on a TTL and on a subject erasure request, and
an outbox row holding the same bbox and image URL must not outlive the row it
mirrors.

WHAT THIS MIGRATION DOES NOT DO: it does not seed a config row. A fresh
appliance has no QA sink, publishes nothing, and the feature is invisible until
somebody fills the form in. Seeding a disabled row with blank brokers would put
a half-configured integration on every install.

RENUMBERED 053 -> 058 when `manual` merged into `field` (2026-10-15): both
branches had shipped a 053/054, and `field`'s (ONVIF-HTTPS, registration) were
already on the `field` fleet. Existence-guarded because a database that ran
the `manual` numbering is re-stamped to 052 by backend/migrate.py and replays
053-059 — these tables are already there on it.

Revision ID: 058
Revises: 057
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "058"
down_revision: Union[str, None] = "057"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("qa_sink_config"):
        return  # ran on the pre-merge `manual` numbering (as 053) — see docstring
    op.create_table(
        "qa_sink_config",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.false()),

        # Where. `bootstrap_servers` is the comma-separated broker list;
        # `kafka_topic` MUST be one the remote consumer subscribes to, which is
        # why the UI says so — a topic it does not subscribe to accepts every
        # message and delivers none, with no error anywhere.
        sa.Column("bootstrap_servers", sa.Text, nullable=False, server_default=""),
        sa.Column("kafka_topic", sa.Text, nullable=False, server_default=""),

        # The `topic` field INSIDE the payload, which is a different thing from
        # the Kafka topic and gates what the consumer does with the event.
        # Keeping them separate columns is deliberate: collapsing them is the
        # single easiest way to get this integration subtly wrong.
        sa.Column("payload_topic", sa.Text, nullable=False,
                  server_default="vms_annotation"),
        sa.Column("severity", sa.String(32), nullable=False, server_default="low"),

        # Optional SASL/PLAIN. The password is Fernet-encrypted at rest by the
        # same crypto.py that holds camera credentials — a broker password is
        # no less of a credential for living in an integration form.
        sa.Column("sasl_mechanism", sa.String(32), nullable=True),
        sa.Column("sasl_username", sa.Text, nullable=True),
        sa.Column("sasl_password_enc", sa.Text, nullable=True),
        sa.Column("use_tls", sa.Boolean, nullable=False, server_default=sa.false()),

        # The image has to be fetchable by a browser that has never heard of
        # this appliance, so it is mirrored to object storage and the payload
        # carries that URL. No bucket means no publishing.
        sa.Column("s3_bucket", sa.Text, nullable=True),
        sa.Column("s3_prefix", sa.Text, nullable=False, server_default=""),
        sa.Column("public_base_url", sa.Text, nullable=True),

        # class_key -> the subcategory string the remote reviewers already read.
        # An unmapped class falls back to its raw key rather than blocking the
        # publish: visibly odd in their UI beats silently dropped here.
        sa.Column("class_map", postgresql.JSONB, nullable=False,
                  server_default="{}"),

        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_qa_sink_config_singleton"),
    )

    op.create_table(
        "qa_publish_outbox",
        sa.Column("annotation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("annotations.id", ondelete="CASCADE"),
                  primary_key=True),
        # The payload is frozen at enqueue time. Rebuilding it at send time
        # would re-read a config that may have changed since, so a retry after
        # an operator edits the class map would publish something different
        # from what the first attempt tried to send.
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("image_key", sa.Text, nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending', 'sent', 'failed')",
            name="ck_qa_publish_outbox_state"),
    )
    # The relay's only query: the due pending rows, oldest first. Without this
    # it scans the whole outbox every few seconds for the rest of time.
    op.create_index(
        "ix_qa_publish_outbox_due", "qa_publish_outbox",
        ["next_attempt_at"], postgresql_where=sa.text("state = 'pending'"))


def downgrade() -> None:
    op.drop_index("ix_qa_publish_outbox_due", table_name="qa_publish_outbox")
    op.drop_table("qa_publish_outbox")
    op.drop_table("qa_sink_config")
