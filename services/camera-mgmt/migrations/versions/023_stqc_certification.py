"""STQC / BIS-ER certification posture per camera (India, mandatory 1 April 2026).

Revision ID: 023
Revises: 022
Create Date: 2026-08-10

Since 1 April 2026 only CCTV cameras holding valid Essential Requirements /
STQC certification may be **sold** in India, against mandated cybersecurity,
storage, and no-unauthorised-offshore-transmission criteria. Every Indian
operator now needs to answer "which cameras in my fleet are compliant?" and no
VMS on the market ships that inventory view.

**Deliberately not a registration gate**, unlike `lawful_basis` in 021. The
mandate restricts what may be *sold*, not what may be *operated* — an existing
fleet of uncertified cameras is lawful to keep running, and refusing to register
one would make the product unusable for exactly the brownfield customer who most
needs the report. So the column records posture, it does not enforce it: the
default is `unknown`, and the value of the feature is making `unknown` visible
and countable rather than pretending it is `certified`.

`unknown` as the server_default is therefore load-bearing and not laziness. A
back-fill guess would be the one outcome worse than no feature at all — an
operator attesting compliance they never verified. Every pre-existing camera
starts as an honest "nobody has checked this", which is what the report should
say.

`stqc_valid_until` is a DATE, not a timestamp: certificates are issued with
calendar validity and no meaningful time-of-day, and storing it as a date keeps
"expires within 90 days" a plain date comparison free of timezone skew.

The expiry index is partial — the vast majority of rows will never carry a date
(unknown/not_applicable cameras), and the report only ever scans the ones that
do.

NOTE: migration 022's docstring says `alert_rules` lands in "023". This took
that number while the event spine is parked; the rules engine becomes 024.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "023"
down_revision: Union[str, None] = "022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # certified | not_certified | exempt | not_applicable | unknown
    # Validated app-side against STQC_STATUSES (models.py) rather than by a CHECK
    # constraint, mirroring how 021 handles lawful_basis: the vocabulary is
    # regulatory and will move, and a migration per vocabulary tweak is a poor
    # trade for a field that gates nothing.
    op.add_column(
        "cameras",
        sa.Column(
            "stqc_status",
            sa.String(32),
            nullable=False,
            server_default="unknown",
        ),
    )
    # Certificate/registration number as printed on the certificate. Free text —
    # the format is not stable across issuing labs, and rejecting a real
    # certificate because it fails our regex is the worse failure.
    op.add_column(
        "cameras", sa.Column("stqc_certificate_no", sa.String(128), nullable=True)
    )
    op.add_column("cameras", sa.Column("stqc_valid_until", sa.Date(), nullable=True))
    # Who attested this and when. The audit log records the mutation; these two
    # keep the attestation legible on the row itself, so the posture report can
    # show "verified by X on Y" without a join against the audit chain.
    op.add_column(
        "cameras",
        sa.Column("stqc_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "cameras", sa.Column("stqc_verified_by", sa.String(255), nullable=True)
    )

    # The report's two hot queries: fleet posture counts, and "expiring soon".
    op.create_index("ix_cameras_stqc_status", "cameras", ["stqc_status"])
    op.create_index(
        "ix_cameras_stqc_expiry",
        "cameras",
        ["stqc_valid_until"],
        postgresql_where=sa.text("stqc_valid_until IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_cameras_stqc_expiry", table_name="cameras")
    op.drop_index("ix_cameras_stqc_status", table_name="cameras")
    op.drop_column("cameras", "stqc_verified_by")
    op.drop_column("cameras", "stqc_verified_at")
    op.drop_column("cameras", "stqc_valid_until")
    op.drop_column("cameras", "stqc_certificate_no")
    op.drop_column("cameras", "stqc_status")
