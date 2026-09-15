"""Per-camera low-resolution sub track.

Revision ID: 032
Revises: 031
Create Date: 2026-09-04

Playback of an HEVC camera costs a transcode, because no browser outside the
Apple family decodes HEVC. Most IP cameras already publish a second, smaller
stream; recording it gives playback something cheaper to serve, and on some
cameras something free.

Probed on this appliance 2026-09-04 (every profile on every recording camera —
the plan's rule is that a substream's codec is NEVER assumed):

    entry-g98a       main hevc 1920x1080  ->  sub h264 1280x720   copy, no ffmpeg
    cam34-n17v       main hevc 1280x720   ->  sub h264  352x288   copy, no ffmpeg
    exit-gate-hvte   main hevc 1920x1080  ->  sub hevc  352x288   20x fewer pixels
    corner-001-u3z2  main hevc 1920x1080  ->  sub hevc  704x576    5x fewer pixels

That corrects the multi-stream plan, which recorded the GPU server's Dahua fleet
as "all-HEVC, no H.264 anywhere" and concluded a `-c copy` path was off the
table. True there; not true here. The two cameras that currently force a
transcode both have a usable sub, and one of them is H.264.

NULL is a valid and complete state: a camera without a sub track records and
plays exactly as it does today. Nothing starts recording as a result of this
migration — `recording_enabled` inside the JSON gates that, and resolution
leaves it false.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "032"
down_revision: Union[str, None] = "031"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column("sub_track", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cameras", "sub_track")
