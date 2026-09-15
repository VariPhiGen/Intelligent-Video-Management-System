"""Per-camera search domains: which of person / vehicles / plate to index.

Revision ID: 031
Revises: 030
Create Date: 2026-08-31

Migration 030 gave each camera an on/off switch. This says WHICH things a camera
contributes, so a gate camera can index vehicles and plates while an office
camera indexes only people.

WHAT THIS DOES AND DOES NOT SAVE, measured 2026-08-31 before building it:

  * NOT detection. The detector's `classes=` filter is applied AFTER the forward
    pass — 120 frames took 1.12 s for person+vehicles, 1.09 s for person only,
    1.08 s unfiltered. All noise. A person-only camera costs what a
    person-and-vehicle camera costs.
  * Embedding of excluded crops, but only when such objects actually appear. On
    an office camera that never sees a car this is nothing, because there was
    never a car crop to skip.
  * Plate reading, genuinely: that is a second model over every vehicle crop, so
    a camera that opts out of plates skips it entirely.
  * STORAGE, genuinely, and this is the real one. Storage is the binding
    constraint (measured 5,670 bytes per row all-in), so not indexing a domain
    you will never search is the saving that matters.

So this is primarily about relevance, privacy and disk — not CPU. The CPU saving
already came from the motion gate, which skips 98-99% of frames outright.

DEFAULT person + vehicles, which is exactly what every camera does today, so
applying this changes nothing until somebody narrows a camera.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "031"
down_revision: Union[str, None] = "030"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None

# "vehicles" is plural to match the wire contract and the table names, where the
# domain has always been spelled that way. Consistency beats symmetry here: a
# mismatch between config and query strings is a bug nobody sees until a search
# silently returns nothing.
DOMAINS = ("person", "vehicles", "plate")


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column(
            "search_domains",
            sa.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("ARRAY['person','vehicles']::text[]"),
        ),
    )
    # Reject a typo at write time rather than discovering it as a camera that
    # silently indexes nothing.
    op.create_check_constraint(
        "cameras_search_domains_valid",
        "cameras",
        "search_domains <@ ARRAY['person','vehicles','plate']::text[]",
    )


def downgrade() -> None:
    op.drop_constraint("cameras_search_domains_valid", "cameras", type_="check")
    op.drop_column("cameras", "search_domains")
