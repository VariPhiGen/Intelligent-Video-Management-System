"""peripheral_bindings and pending_actions — how a device is reached, and the
follow-up half of a pulse.

Revision ID: 040
Revises: 039
Create Date: 2026-09-18

WHY BINDINGS ARE NOT COLUMNS ON `peripherals`.

Migration 029 stated the principle that the peripherals row holds what a PERSON
can know — what it is, where it is, what it is called. A binding is what a
MACHINE needs: a driver, an address, a payload template, credentials. Keeping
them apart preserves 029's principle, leaves the peripherals API surface
unchanged for operators who only curate the inventory, and lets a binding be
replaced without touching the map pins that FK to the peripheral.

The primary key IS `peripheral_id`, so one device has at most one binding. Two
would mean two answers to "where does this live", and the second would be
silently unused.

WHY PENDING ACTIONS EXIST.

`for_seconds`/`then` — "open the barrier for 8 seconds" — is two commands. The
API runs four uvicorn workers, so the worker that opened it may be gone before
the close is due. The follow-up is therefore a committed row, written BEFORE the
first half executes, so a crash between the two leaves evidence rather than
silence.

This is defence in depth, NOT the safety mechanism. The PLC owns the timeout
that closes the barrier (spec 11). What this buys is that a worker restart
during a deploy does not leave a gate open with no record of why.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "040"
down_revision: Union[str, None] = "039"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "peripheral_bindings",
        # PK, not just FK: one peripheral, at most one binding.
        sa.Column("peripheral_id", sa.BigInteger(),
                  sa.ForeignKey("peripherals.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("driver", sa.String(24), nullable=False),
        # MQTT command topic | HA entity_id | webhook URL.
        sa.Column("address", sa.String(255), nullable=False),
        # Payload templates, QoS, HA service name. Driver-specific by design:
        # a column per driver knob would be a migration per driver.
        sa.Column("config", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        # MQTT only. NULL = a write-only device, which is a real thing: a
        # relay that reports nothing back is still worth driving.
        sa.Column("state_topic", sa.String(255), nullable=True),
        # Per-binding credentials, Fernet ciphertext via backend/crypto.py --
        # NEVER plaintext. Same path camera credentials already use, and there
        # is deliberately no plaintext column to read instead by mistake.
        sa.Column("secret_enc", sa.Text(), nullable=True),
        # Last time the bridge CONFIRMED this address resolves. NULL means
        # never confirmed, and an unverified binding refuses to actuate: HA
        # entity ids move between releases (2026.5 broke several integrations
        # exactly this way) and a silent no-op at 2am is the worst outcome.
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verify_error", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("driver IN ('mqtt','homeassistant','webhook')",
                           name="ck_bindings_driver"),
        # A state topic equal to the command topic is a message loop: the
        # bridge would hear its own command and treat it as device state. The
        # wildcard forms are caught in validate_topics, which a CHECK cannot
        # express; this catches the one that is pure equality.
        sa.CheckConstraint("state_topic IS NULL OR state_topic <> address",
                           name="ck_bindings_no_loop"),
    )

    op.create_table(
        "pending_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Nullable: a manual pulse from the Status panel has no alert behind it.
        sa.Column("alert_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("alerts.id", ondelete="CASCADE"), nullable=True),
        sa.Column("peripheral_id", sa.BigInteger(),
                  sa.ForeignKey("peripherals.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("command", sa.String(40), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), nullable=False,
                  server_default="0"),
        sa.Column("state", sa.String(16), nullable=False,
                  server_default="pending"),
        sa.Column("last_error", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "state IN ('pending','claimed','done','failed','abandoned')",
            name="ck_pending_state"),
    )
    # The sweep's only query: what is due and still pending.
    op.execute("CREATE INDEX ix_pending_due ON pending_actions (due_at) "
               "WHERE state = 'pending'")


def downgrade() -> None:
    op.drop_index("ix_pending_due", table_name="pending_actions")
    op.drop_table("pending_actions")
    op.drop_table("peripheral_bindings")
