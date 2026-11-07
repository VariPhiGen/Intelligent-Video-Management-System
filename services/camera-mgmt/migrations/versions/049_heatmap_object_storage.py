"""heatmap object storage: the `live` kind and its one-row-per-session rule

Two changes, both to `heatmap_artifacts`, neither touching existing rows.

1. `kind` gains `live`. There are now three kinds with three different lives:
   `live` is the picture the page shows right now, replaced every ~30 s and
   never archived; `snapshot` is a fixed half-hour wall-clock boundary; `final`
   is the picture a session ended on. Only the last two are kept.

2. A PARTIAL UNIQUE INDEX on `(session_id) WHERE kind = 'live'`.

   This is the load-bearing one. The existing uniqueness is
   `(session_id, kind, captured_at)`, which is right for an archived picture —
   each capture is its own record, and a retried upload of the same capture
   collapses onto it. It is wrong for `live`, whose `captured_at` moves every
   30 seconds: without this index each live update INSERTS rather than
   replaces, at roughly 2,880 rows per camera per day, none of which anyone
   reads after the next one lands. With it, a session has exactly one live row
   and the ingest upserts in place.

`object_key` needs no migration: migration 041 already created it as the
reserved seam for an object store, and it has been null ever since. Rows keep
their inline bytes; nothing is backfilled. The read path serves whichever of
the two is present, so the old and new storage paths coexist.

Revision ID: 049
Revises: 048
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "049"
down_revision: Union[str, None] = "048"
branch_labels = None
depends_on = None

_KIND_CK = "ck_heatmap_artifacts_kind"
_LIVE_UX = "ux_heatmap_artifacts_live"


def upgrade() -> None:
    op.drop_constraint(_KIND_CK, "heatmap_artifacts", type_="check")
    op.create_check_constraint(
        _KIND_CK, "heatmap_artifacts", "kind IN ('live','snapshot','final')")
    op.create_index(
        _LIVE_UX, "heatmap_artifacts", ["session_id"],
        unique=True, postgresql_where=sa.text("kind = 'live'"),
    )


def downgrade() -> None:
    # Live rows cannot satisfy the old constraint, so they go first. They are
    # by definition the current picture of a running session and are recreated
    # within 30 seconds; nothing historical is lost.
    op.execute("DELETE FROM heatmap_artifacts WHERE kind = 'live'")
    op.drop_index(_LIVE_UX, table_name="heatmap_artifacts")
    op.drop_constraint(_KIND_CK, "heatmap_artifacts", type_="check")
    op.create_check_constraint(
        _KIND_CK, "heatmap_artifacts", "kind IN ('snapshot','final')")
