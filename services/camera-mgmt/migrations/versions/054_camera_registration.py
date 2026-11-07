"""Camera-to-plan registration: what a camera SEES, not just where it is.

RENUMBERED on the field -> ai merge (was 050); see backend/migrate.py.

Revision ID: 054
Revises: 053
Create Date: 2026-09-28

Camera placement on a sitemap is a pin today — `camera_metadata.sitemap`
carries `{id, x, y}` and nothing else — so the Map tab knows where a camera
stands and nothing about the ground it covers. This table adds the missing
half: a homography fitted from landmark pairs an operator clicks, which turns
any ground-plane point in the camera's frame into a point on the plan and back.

That unlocks the thing zones actually need. A detection region drawn once on
the plan can be projected into every camera that covers it, instead of being
redrawn per camera as an unrelated polygon that merely happens to mean the same
place (plan Section 22.7).

NUMBERING: this revision deliberately jumps from 036 to 050, following the
precedent 035 set when it skipped 034. The `field` and `ai` branches have
already diverged over ids 033, 035 and 036 — each number names a *different*
migration on each side — and `ai` has since reached 040. Taking 037 here would
add a fourth collision to that set for no gain, because an id is only a name:
alembic follows `down_revision`, and the gap costs nothing. 037-049 are left as
headroom for `ai` so the eventual merge has one less thing to renumber.

`preset_token` is NOT NULL with an empty-string default rather than nullable,
and that is load-bearing in two ways. Postgres treats NULLs as distinct in a
unique index, so a nullable column would let the same camera be registered
against the same plan twice with no constraint violation — exactly the
duplicate the unique constraint below exists to prevent. And the column being
present from the start is what makes PTZ multi-preset registration (plan
Section 22.8 stage C) add *rows* rather than a schema change: '' means the
fixed view every camera has today, and a real ONVIF preset token means one
recallable view of a PTZ.

Both foreign keys cascade. A registration describes the relationship between
one camera and one plan, so it is meaningless once either is gone — and a stale
row would be worse than absent, since the map would draw a coverage footprint
for a camera that no longer exists.

`footprint` is stored rather than recomputed per request. It is a pure function
of the homography, so this is a cache; it is kept because the Map tab draws
every camera's coverage on a plan at once, and because storing it means a
future change to the projection code cannot silently redraw historical coverage
without a visible migration.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# Idempotent on purpose (merge of `field` into the ai chain, 2026-10-07): appliances that ran the
# old `field` numbering already HAVE what this migration creates. backend/migrate.py re-stamps them onto
# the merged chain at 032 and replays forward, so every step that can meet an existing object checks first.
def _insp():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _insp().has_table(name)


def _has_column(table: str, column: str) -> bool:
    return _has_table(table) and any(c["name"] == column for c in _insp().get_columns(table))


def _has_index(table: str, index: str) -> bool:
    return _has_table(table) and any(i["name"] == index for i in _insp().get_indexes(table))

revision: str = "054"
down_revision: Union[str, None] = "053"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not _has_table("camera_registrations"):
        op.create_table(
            "camera_registrations",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("camera_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("sitemap_id", sa.BigInteger(), nullable=False),
            # '' = the camera's fixed/current view. A non-empty value is an ONVIF
            # preset token, so a PTZ carries one row per calibrated preset.
            sa.Column("preset_token", sa.String(length=64), nullable=False,
                      server_default=sa.text("''")),
            # 3x3 row-major, FRAME -> PLAN. The opposite direction is derived with
            # a matrix inverse at use time rather than stored, so the two can never
            # drift out of step with each other.
            sa.Column("homography", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            # The operator's clicked pairs, kept rather than discarded after the
            # fit: they are what lets a registration be re-fitted, audited, or
            # checked for drift when the site changes (plan Section 22.5).
            sa.Column("landmark_pairs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            # RMS error in normalized plan units. Converts to metres only where the
            # sitemap carries `calibration`, so anything showing this to a person
            # must say which unit it is displaying.
            sa.Column("reprojection_error", sa.Float(), nullable=False),
            sa.Column("footprint", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("calibrated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("calibrated_by", sa.String(length=255), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["sitemap_id"], ["sitemaps.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("camera_id", "sitemap_id", "preset_token",
                                name="uq_camera_registration_view"),
        )
        # The Map tab's primary read is "every registration on this plan", to draw
        # all coverage footprints in one pass.
    if not _has_index("camera_registrations", "ix_camera_registrations_sitemap_id"):
        op.create_index("ix_camera_registrations_sitemap_id", "camera_registrations",
                        ["sitemap_id"])
        # The camera detail panel's read is "every plan this camera is registered
        # against" — rarer, but it is also what the cascade walks on delete.
    if not _has_index("camera_registrations", "ix_camera_registrations_camera_id"):
        op.create_index("ix_camera_registrations_camera_id", "camera_registrations",
                        ["camera_id"])


def downgrade() -> None:
    op.drop_index("ix_camera_registrations_camera_id", table_name="camera_registrations")
    op.drop_index("ix_camera_registrations_sitemap_id", table_name="camera_registrations")
    op.drop_table("camera_registrations")
