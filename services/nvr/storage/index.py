"""
Segment Index — SQLite-backed mapping from (camera, timestamp) to segment files.

Design rationale:
- SQLite is the index, not the source of truth. The filesystem is authoritative.
  If the DB is lost, it can be rebuilt by scanning segment files and probing durations.
- We store start_time as a REAL (Unix epoch) for fast range queries.
- WAL journal mode allows concurrent reads (API) while writes (recorder) proceed.
- Each segment row stores: camera, start epoch, duration, filepath, file size.

Why not just use filenames?
  Filenames encode the start time (strftime), which works for lookup. But we also
  need exact durations (segments may be shorter than target on reconnect/shutdown)
  and gap detection. SQLite gives us sub-millisecond range queries on indexed columns.
"""

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Segment:
    camera: str
    start_epoch: float
    duration: float
    filepath: str
    file_size: int

    @property
    def end_epoch(self) -> float:
        return self.start_epoch + self.duration


_SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera TEXT NOT NULL,
    start_epoch REAL NOT NULL,
    duration REAL NOT NULL,
    filepath TEXT NOT NULL UNIQUE,
    file_size INTEGER NOT NULL DEFAULT 0,
    groomed INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_camera_start
    ON segments (camera, start_epoch);

CREATE INDEX IF NOT EXISTS idx_camera_end
    ON segments (camera, start_epoch + duration);
"""


class SegmentIndex:
    """Thread-safe segment index backed by SQLite."""

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._local = threading.local()
        # Initialize schema on first connection
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            # In-place migration for DBs created before the groomed column.
            try:
                conn.execute(
                    "ALTER TABLE segments ADD COLUMN groomed INTEGER NOT NULL DEFAULT 0"
                )
                conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists

    @contextmanager
    def _conn(self):
        """Get a thread-local connection with WAL mode."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self._db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        yield self._local.conn

    def add_segment(self, seg: Segment) -> None:
        """Register a completed segment in the index."""
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO segments
                   (camera, start_epoch, duration, filepath, file_size)
                   VALUES (?, ?, ?, ?, ?)""",
                (seg.camera, seg.start_epoch, seg.duration,
                 seg.filepath, seg.file_size),
            )
            conn.commit()

    def find_segments(self, camera: str, from_epoch: float,
                      to_epoch: float) -> list[Segment]:
        """Find all segments for a camera that overlap [from_epoch, to_epoch].

        A segment overlaps if its time range [start, start+duration] intersects
        the query range [from, to]. We use inclusive (`<=` / `>=`) boundaries:
          segment.start <= to_epoch AND segment.start + segment.duration >= from_epoch
        Strict inequalities used to drop segments that touch the boundary
        exactly — e.g. asking for [latest, latest+60] when `latest` equals a
        segment's end_epoch returned no segments and the API surfaced a
        spurious 404. The point case `find_segments(cam, t, t)` likewise
        needs inclusive bounds to find the segment that starts or ends at
        exactly `t` (used by /snapshot for boundary timestamps).
        """
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT camera, start_epoch, duration, filepath, file_size
                   FROM segments
                   WHERE camera = ?
                     AND start_epoch <= ?
                     AND (start_epoch + duration) >= ?
                   ORDER BY start_epoch ASC""",
                (camera, to_epoch, from_epoch),
            ).fetchall()
        return [
            Segment(
                camera=r["camera"],
                start_epoch=r["start_epoch"],
                duration=r["duration"],
                filepath=r["filepath"],
                file_size=r["file_size"],
            )
            for r in rows
        ]

    def find_by_filename(self, camera: str, filename: str) -> Segment | None:
        """One indexed segment, looked up by its bare filename.

        The HLS routes receive a segment stem in the URL and need the row, not
        just the file: the row is what proves the segment is still part of the
        recording (footage erased under a DSR leaves the index, so a cached
        artifact stops being reachable the moment its row goes) and it carries
        the duration the transcode timeout is sized from.

        Matched on the path suffix rather than a reconstructed absolute path so
        this stays correct if the storage layout ever changes. `filename` is
        always a validated stem plus ``.ts``, so it cannot smuggle a LIKE
        wildcard. Measured at ~1.3 ms against a 50k-row index — this runs once
        per minute of playback, not per frame.
        """
        with self._conn() as conn:
            row = conn.execute(
                """SELECT camera, start_epoch, duration, filepath, file_size
                     FROM segments
                    WHERE camera = ? AND filepath LIKE ?
                    ORDER BY start_epoch DESC
                    LIMIT 1""",
                (camera, f"%/{filename}"),
            ).fetchone()
        if row is None:
            return None
        return Segment(
            camera=row["camera"],
            start_epoch=row["start_epoch"],
            duration=row["duration"],
            filepath=row["filepath"],
            file_size=row["file_size"],
        )

    def get_recording_range(self, camera: str) -> tuple[float, float] | None:
        """Return (earliest_start, latest_end) for a camera, or None."""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT MIN(start_epoch) as earliest,
                          MAX(start_epoch + duration) as latest
                   FROM segments WHERE camera = ?""",
                (camera,),
            ).fetchone()
        if row and row["earliest"] is not None:
            return (row["earliest"], row["latest"])
        return None

    def get_cameras(self) -> list[str]:
        """Return list of all camera names with recorded segments."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT camera FROM segments ORDER BY camera"
            ).fetchall()
        return [r["camera"] for r in rows]

    def get_indexed_filepaths_since(self, camera: str,
                                     since_epoch: float) -> set[str]:
        """Return the set of indexed filepaths for a camera with
        start_epoch >= since_epoch.

        Used by the reconciliation pass to set-diff against the filesystem
        and find segments missing from the index.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT filepath FROM segments
                   WHERE camera = ? AND start_epoch >= ?""",
                (camera, since_epoch),
            ).fetchall()
        return {r["filepath"] for r in rows}

    def delete_before(self, camera: str, before_epoch: float) -> list[str]:
        """Delete segments older than before_epoch. Returns deleted filepaths."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT filepath FROM segments
                   WHERE camera = ? AND (start_epoch + duration) < ?""",
                (camera, before_epoch),
            ).fetchall()
            paths = [r["filepath"] for r in rows]
            if paths:
                conn.execute(
                    """DELETE FROM segments
                       WHERE camera = ? AND (start_epoch + duration) < ?""",
                    (camera, before_epoch),
                )
                conn.commit()
        return paths

    def select_oldest_until_under(
        self, target_bytes: int,
    ) -> list[tuple[int, str, int]]:
        """Return (id, filepath, file_size) for the globally-oldest segments
        whose combined size brings total indexed bytes at or below
        target_bytes. Does NOT modify the index — the caller must unlink the
        files on disk and then call delete_by_ids() for the rows whose
        unlink succeeded. Two-step ordering keeps DB↔FS in sync under IO
        faults: if unlink fails (EROFS, NFS hiccup), the row stays in the
        index and the next retention pass retries it.

        Streams from a cursor rather than fetchall() so memory stays flat at
        scale. Concurrent writes from StreamWorkers are safe — the caller
        only deletes IDs we've already enumerated.
        """
        with self._conn() as conn:
            current = conn.execute(
                "SELECT COALESCE(SUM(file_size), 0) AS total FROM segments"
            ).fetchone()["total"]
            if current <= target_bytes:
                return []
            to_free = current - target_bytes

            candidates: list[tuple[int, str, int]] = []
            freed = 0
            cursor = conn.execute(
                """SELECT id, filepath, file_size FROM segments
                   ORDER BY start_epoch ASC"""
            )
            for row in cursor:
                if freed >= to_free:
                    break
                candidates.append((row["id"], row["filepath"], row["file_size"]))
                freed += row["file_size"]
        return candidates

    def select_overlapping(
        self, camera: str, from_epoch: float, to_epoch: float,
    ) -> list[tuple[int, float, float, str, int]]:
        """Rows (id, start_epoch, duration, filepath, file_size) that overlap
        [from_epoch, to_epoch]. Does NOT modify the index — the scoped-erasure
        caller partitions into fully-inside (deletable) vs boundary-overlap
        (preserved), unlinks the files, then calls delete_by_ids() for rows
        whose unlink succeeded (same DB↔FS ordering as the size cap)."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT id, start_epoch, duration, filepath, file_size
                   FROM segments
                   WHERE camera = ?
                     AND start_epoch <= ?
                     AND (start_epoch + duration) >= ?
                   ORDER BY start_epoch ASC""",
                (camera, to_epoch, from_epoch),
            ).fetchall()
        return [
            (r["id"], r["start_epoch"], r["duration"], r["filepath"],
             r["file_size"])
            for r in rows
        ]

    def delete_by_ids(self, ids: list[int]) -> None:
        """Delete index rows by primary key. Chunked to stay under SQLite's
        ~999-parameter limit."""
        if not ids:
            return
        with self._conn() as conn:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                placeholders = ",".join("?" * len(chunk))
                conn.execute(
                    f"DELETE FROM segments WHERE id IN ({placeholders})",
                    chunk,
                )
            conn.commit()

    def select_groomable(self, before_epoch: float, limit: int = 500,
                         camera: str | None = None) -> list[tuple[int, str, str, int]]:
        """Segments fully older than before_epoch that haven't been groomed,
        optionally filtered by camera (per-camera groom thresholds).

        Returns (id, camera, filepath, file_size), oldest first, capped at
        ``limit`` so one nightly pass stays bounded.
        """
        with self._conn() as conn:
            if camera:
                rows = conn.execute(
                    """SELECT id, camera, filepath, file_size FROM segments
                       WHERE camera = ? AND groomed = 0 AND (start_epoch + duration) < ?
                       ORDER BY start_epoch ASC LIMIT ?""",
                    (camera, before_epoch, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, camera, filepath, file_size FROM segments
                       WHERE groomed = 0 AND (start_epoch + duration) < ?
                       ORDER BY start_epoch ASC LIMIT ?""",
                    (before_epoch, limit),
                ).fetchall()
        return [(r["id"], r["camera"], r["filepath"], r["file_size"]) for r in rows]

    def mark_groomed(self, seg_id: int, new_size: int) -> None:
        """Record a successful groom: new on-disk size + never process again."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE segments SET groomed = 1, file_size = ? WHERE id = ?",
                (new_size, seg_id),
            )
            conn.commit()

    def total_size(self, camera: str | None = None) -> int:
        """Total bytes stored, optionally filtered by camera."""
        with self._conn() as conn:
            if camera:
                row = conn.execute(
                    "SELECT COALESCE(SUM(file_size), 0) as total FROM segments WHERE camera = ?",
                    (camera,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COALESCE(SUM(file_size), 0) as total FROM segments"
                ).fetchone()
        return row["total"]

    def size_split(self, camera: str | None = None) -> tuple[int, int]:
        """(normal_bytes, cold_bytes) — full-quality (ungroomed) vs groomed
        keyframe-only footage. Optionally filtered by camera."""
        sql = (
            "SELECT "
            "COALESCE(SUM(CASE WHEN groomed = 0 THEN file_size ELSE 0 END), 0) AS normal, "
            "COALESCE(SUM(CASE WHEN groomed = 1 THEN file_size ELSE 0 END), 0) AS cold "
            "FROM segments"
        )
        with self._conn() as conn:
            if camera:
                row = conn.execute(sql + " WHERE camera = ?", (camera,)).fetchone()
            else:
                row = conn.execute(sql).fetchone()
        return row["normal"], row["cold"]

    def range_split(
        self, camera: str | None = None
    ) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
        """(normal_range, cold_range) — each is (earliest_start, latest_end)
        for that tier, or None if the tier has no footage. Optionally
        filtered by camera."""
        sql = (
            "SELECT groomed, "
            "MIN(start_epoch) AS earliest, "
            "MAX(start_epoch + duration) AS latest "
            "FROM segments"
        )
        with self._conn() as conn:
            if camera:
                rows = conn.execute(
                    sql + " WHERE camera = ? GROUP BY groomed", (camera,)
                ).fetchall()
            else:
                rows = conn.execute(sql + " GROUP BY groomed").fetchall()
        ranges: dict[int, tuple[float, float]] = {
            r["groomed"]: (r["earliest"], r["latest"])
            for r in rows if r["earliest"] is not None
        }
        return ranges.get(0), ranges.get(1)

    def rebuild_from_filesystem(self, storage_path: Path, probe_fn) -> int:
        """Rebuild index by scanning segment files. Returns count of segments indexed.

        probe_fn(filepath) -> (start_epoch, duration) or None
        """
        count = 0
        for ts_file in sorted(storage_path.rglob("*.ts")):
            result = probe_fn(str(ts_file))
            if result is None:
                continue
            start_epoch, duration = result
            camera = ts_file.parent.name
            seg = Segment(
                camera=camera,
                start_epoch=start_epoch,
                duration=duration,
                filepath=str(ts_file),
                file_size=ts_file.stat().st_size,
            )
            self.add_segment(seg)
            count += 1
        return count
