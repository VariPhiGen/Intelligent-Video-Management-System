"""store.py — the index database.

Reachability, schema state, and the ingest write path.

The service must start and serve /health whether or not the database is up,
because "the search database is down" is something an operator needs the service
to TELL them, not something it should crash over. Writes fail the same way: a
batch that cannot be written is counted and logged, never raised into the
capture thread.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

# ONE definition of what a half-written crop is called. The sweep below has to
# recognise exactly what writer.py leaves behind, and two spellings of ".part"
# is how the sweep quietly stops collecting them.
from .domains import ALL_TABLES, DOMAINS, ERASE_KEYS, FRAME_COLUMNS, TABLES, spec
from .writer import PART_SUFFIX as CROP_PART_SUFFIX

log = logging.getLogger("smartsearch.store")


class Store:
    def __init__(self, dsn: str, retention_days: int = 30) -> None:
        self._dsn = dsn
        self._retention = timedelta(days=retention_days)
        # Per-camera override of the appliance default, pushed by the registry
        # (POST /cameras/{slug}?retention_days=). A camera set to keep two days
        # of footage must not leave thirty days of crops cut from it behind:
        # the crop IS the footage as far as a data subject is concerned, and a
        # deletion promise the operator made about a camera has to bind
        # everything derived from it. Kept in memory, not in a table, because
        # the registry re-asserts it on every reconcile pass — the same reason
        # the camera list itself is not persisted here.
        self._camera_retention: dict[str, timedelta] = {}
        # write_batch runs on ingest threads while the registry writes this from
        # the API thread; a plain dict read during a rebind is not safe.
        self._retention_lock = threading.Lock()
        # Opened lazily and never at import: the service has to boot with the
        # database down.
        self._pool: ConnectionPool | None = None
        self.rows_written = 0
        self.write_failures = 0

    #: Restamp batch size. Same bound and reason as ERASE_BATCH / EXPIRE_BATCH.
    RESTAMP_BATCH = 5_000

    def retention_for(self, camera: str) -> timedelta:
        """This camera's retention, or the appliance default if it has none."""
        with self._retention_lock:
            return self._camera_retention.get(camera, self._retention)

    def set_camera_retention(self, camera: str, days: int | None) -> dict[str, Any]:
        """Adopt a camera's retention and RESTAMP the rows already indexed for it.

        Restamping is the whole point, and stamping new rows alone would be a
        quiet no-op for the case operators actually hit: retention is normally
        shortened AFTER data exists, to comply with something. `expires_at` is
        written once at insert, so without this pass, dropping a camera from
        thirty days to two would leave every crop already taken from it sitting
        for another month while the UI reports two — the appliance asserting a
        deletion deadline it is not keeping.

        Bidirectional on purpose. Lengthening retention restamps too, so a
        correction to a policy set wrongly is not one-way; nothing is deleted
        here either way, only the deadline moves, and expire_once acts on it.

        Never raises. This is called from camera registration, and a camera that
        cannot be registered because the search database is briefly down is a
        worse outcome than a stale deadline the next reconcile pass fixes.
        """
        interval = timedelta(days=days) if days else None
        with self._retention_lock:
            previous = self._camera_retention.get(camera)
            if interval is None:
                self._camera_retention.pop(camera, None)
            else:
                self._camera_retention[camera] = interval
            effective = interval or self._retention
        if previous == interval:
            # Every reconcile pass re-asserts every camera. Restamping on each
            # one would be an UPDATE over the whole index every sync interval.
            return {"rows_restamped": 0, "changed": False, "error": None}
        out: dict[str, Any] = {"rows_restamped": 0, "changed": True, "error": None}
        try:
            for table, cam_col in TABLES:
                while True:
                    with self._get_pool().connection() as conn, conn.cursor() as cur:
                        # BATCHED, for ERASE_BATCH's reason. A camera thirty days
                        # into a busy site has millions of rows, and restamping
                        # them in one statement holds a transaction and their row
                        # locks open across the whole set — with ingest for that
                        # same camera queued behind it. Retention changes are rare
                        # and never urgent; blocking the write path is not a
                        # trade worth making for them.
                        #
                        # `expires_at <> ts + interval` is what terminates the
                        # loop: each batch stops matching once it is rewritten.
                        # It also makes the whole call a no-op once converged, so
                        # a restart that re-asserts every camera does not rewrite
                        # rows that already carry the right deadline.
                        cur.execute(
                            f"UPDATE {table} SET expires_at = ts + %(iv)s"
                            f" WHERE id IN ("
                            f"   SELECT id FROM {table}"
                            f"    WHERE {cam_col} = %(cam)s"
                            f"      AND expires_at <> ts + %(iv)s"
                            f"    LIMIT {self.RESTAMP_BATCH})",
                            {"iv": effective, "cam": camera},
                        )
                        n = cur.rowcount
                    if not n:
                        break
                    out["rows_restamped"] += n
        except psycopg.Error as exc:
            out["error"] = str(exc).strip().splitlines()[0][:200]
            log.warning("store.retention.restamp_failed camera=%s: %s",
                        camera, out["error"])
            return out
        log.info("store.retention camera=%s days=%s rows_restamped=%d",
                 camera, days if days else "default", out["rows_restamped"])
        return out

    def _get_pool(self) -> ConnectionPool:
        if self._pool is None:
            self._pool = ConnectionPool(self._dsn, min_size=1, max_size=4,
                                        open=True, timeout=10)
        return self._pool

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def fetch(self, sql: str, params: dict | None = None) -> list[dict]:
        """Read query, rows as dicts. Raises — unlike the write path.

        A failed READ must reach the caller: the API turns it into a 5xx the
        operator can see. Swallowing it would return an empty result set, which
        reads as "nothing matched" and is the one answer a search must never
        give wrongly.
        """
        with self._get_pool().connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params or {})
                return cur.fetchall()

    def write_batch(self, domain: str, rows: Sequence[dict]) -> int:
        """Insert a batch of crops. Returns how many landed; never raises.

        A write failure must not propagate: the capture threads that feed this
        are what recover the pipeline, and killing them to report a transient
        database error trades a gap in the index for an outage.
        """
        if not rows:
            return 0
        # The domain's shape comes from the registry, not from a branch here.
        # Two domains fitted in an if/else; the third is what turns that into a
        # place where one table quietly stops being written. See index/domains.py.
        try:
            d = spec(domain)
        except KeyError:
            log.warning("store.write_batch: unknown domain %r, %d row(s) dropped",
                        domain, len(rows))
            self.write_failures += len(rows)
            return 0
        # tracker_id is an extra on every table: persons carried the column from
        # 001 and never had it written; vehicles gained it in 004. See
        # index/tracking.py for why it is a scoped string rather than a counter.
        # The frame columns come last, and only where the domain keeps frames.
        columns = ("embedding", d.camera_column, "ts", "confidence", "bbox",
                   "crop_path", "expires_at") + d.extras + (
                       FRAME_COLUMNS if d.frames else ())
        sql = (f"INSERT INTO {d.table} ({', '.join(columns)}) "
               f"VALUES ({', '.join(['%s'] * len(columns))})")
        params = []
        for r in rows:
            ts: datetime = r["ts"]
            row = [
                r["embedding"], r["camera"], ts, r.get("confidence"),
                r.get("bbox"), r["crop_path"], ts + self.retention_for(r["camera"]),
            ]
            # Extras in declaration order, which is also the INSERT order. A row
            # dict that lacks one writes NULL rather than shifting the columns —
            # the bug migration 003 exists to remember, where plate_confidence
            # was produced for weeks and silently dropped.
            row.extend(r.get(name) for name in d.extras)
            if d.frames:
                # Last of all, matching the column list: the whole frame this
                # observation came from and what was in it, for the feed. NULL
                # from older producers.
                row.append(r.get("frame_path"))
                boxes = r.get("frame_boxes")
                row.append(Jsonb(boxes) if boxes else None)
            params.append(tuple(row))
        try:
            with self._get_pool().connection() as conn, conn.cursor() as cur:
                cur.executemany(sql, params)
            self.rows_written += len(params)
            return len(params)
        except psycopg.Error as exc:
            self.write_failures += len(params)
            log.warning("store.write_failed domain=%s rows=%d: %s",
                        domain, len(params), str(exc).strip().splitlines()[0][:200])
            return 0

    # Erasure batch size. Same bound and same reason as scripts/retention.py: a
    # wide window must not hold one transaction open across the whole table.
    ERASE_BATCH = 2_000

    def erase_range(self, camera: str, start: datetime, end: datetime) -> dict[str, Any]:
        """Delete every indexed crop for `camera` within [start, end]. RAISES.

        The deliberate exception to write_batch's never-raise rule. This backs a
        data-subject erasure, and a swallowed failure here does not cost a gap in
        an index — it turns a legal statement ("this person's footage has been
        destroyed") into a false one. The caller must be able to tell the
        difference between "erased" and "could not", so failure propagates.

        Row-first, the same ordering scripts/retention.py argues for: unlinking
        the file first and then failing to delete the row leaves a searchable hit
        whose crop 404s, and the index goes on asserting the person was there.
        Row-first can strand a JPEG at worst, which ``--sweep-orphans`` reclaims.

        A stranded JPEG is still an un-erased image of a person, though, so
        unlink failures are counted and returned rather than logged and
        forgotten: the caller treats a non-empty ``crops_failed`` as an
        incomplete erasure. A file that is already gone is not a failure.

        Bounded, so the window may be arbitrarily wide. Idempotent: erasing the
        same range twice deletes nothing the second time and still succeeds.
        """
        result: dict[str, Any] = {"crops_unlinked": 0, "crops_failed": 0,
                                  "frames_unlinked": 0, "frames_failed": 0}
        # Every domain starts at zero, so "this domain deleted nothing" and
        # "this domain was never swept" cannot be confused by a reader.
        result.update({key: 0 for key in ERASE_KEYS})
        # EVERY domain, from the registry. An erasure that skips a table
        # reports success with the subject still searchable — see
        # index/domains.py for why.
        for d in DOMAINS.values():
            table, cam_col = d.table, d.camera_column
            key = d.result_key
            # NULL where the table keeps no frame, so every domain returns the
            # same row shape and the unlink loops below need no branch.
            frame_col = "frame_path" if d.frames else "NULL"
            while True:
                # (cam_col, ts DESC) is indexed — search_persons_scope /
                # search_vehicles_scope — so this is a range scan, not a table
                # scan, however much history the index holds.
                sql = (
                    f"DELETE FROM {table} WHERE id IN ("
                    f"  SELECT id FROM {table}"
                    f"   WHERE {cam_col} = %(camera)s AND ts >= %(start)s AND ts <= %(end)s"
                    f"   LIMIT {self.ERASE_BATCH})"
                    f" RETURNING crop_path, {frame_col}"
                )
                with self._get_pool().connection() as conn, conn.cursor() as cur:
                    cur.execute(sql, {"camera": camera, "start": start, "end": end})
                    got = cur.fetchall()
                    paths = [r[0] for r in got]
                    frames = [r[1] for r in got if r[1]]
                if not paths:
                    break
                result[key] += len(paths)
                for raw in paths:
                    try:
                        Path(raw).unlink()
                        result["crops_unlinked"] += 1
                    except FileNotFoundError:
                        # Already gone (retention, a previous erase, a manual
                        # sweep). The image does not exist, which is the outcome
                        # being asked for — not a failure.
                        pass
                    except OSError as exc:
                        result["crops_failed"] += 1
                        log.error("store.erase.unlink_failed path=%s: %s", raw, exc)
                # The WHOLE SCENE, other people in it included. An erasure that
                # left it would have destroyed the crop of a person while
                # keeping a picture of them. A frame is shared by every object
                # found in it, so a later row's unlink finds it already gone —
                # which is the outcome being asked for.
                for raw in frames:
                    try:
                        Path(raw).unlink()
                        result["frames_unlinked"] += 1
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        result["frames_failed"] += 1
                        log.error("store.erase.frame_unlink_failed path=%s: %s", raw, exc)
        log.info(
            "store.erase camera=%s from=%s to=%s rows=%s "
            "crops_unlinked=%d crops_failed=%d frames_unlinked=%d frames_failed=%d",
            camera, start.isoformat(), end.isoformat(),
            " ".join(f"{k}={result[k]}" for k in ERASE_KEYS),
            result["crops_unlinked"], result["crops_failed"],
            result["frames_unlinked"], result["frames_failed"],
        )
        return result

    # Expiry sweep batch size. Same bound and reason as ERASE_BATCH: a
    # long-overdue sweep must not hold one transaction open across millions of
    # rows and block ingest behind it.
    EXPIRE_BATCH = 5_000

    def expire_once(self) -> dict[str, Any]:
        """Delete rows past `expires_at` and unlink their crops. Never raises.

        The counterpart to erase_range, and the opposite failure policy on
        purpose. Erasure backs a legal claim and must propagate; this is
        housekeeping running on a timer, and an exception here would kill the
        thread that is the only thing keeping the index bounded — turning one
        failed sweep into permanent growth. So it logs, counts, and returns.

        Row-first, for the reason scripts/retention.py gives: deleting the file
        first and then failing to delete the row leaves a hit that 404s when the
        operator clicks it, while row-first can only strand a JPEG, which
        sweep_orphans reclaims. Prefer the failure nobody can see in the product.

        This is the ONE implementation. scripts/retention.py calls it too rather
        than keeping its own copy — two versions of a row-first delete is exactly
        how the ordering silently stops holding in one of them.
        """
        out: dict[str, Any] = {"rows_deleted": 0, "crops_unlinked": 0,
                               "crops_failed": 0, "frames_unlinked": 0,
                               "error": None}
        try:
            for d in DOMAINS.values():
                table = d.table
                # See erase_range: NULL where the table keeps no frame.
                frame_col = "frame_path" if d.frames else "NULL"
                while True:
                    with self._get_pool().connection() as conn, conn.cursor() as cur:
                        cur.execute(
                            f"""DELETE FROM {table}
                                 WHERE id IN (SELECT id FROM {table}
                                               WHERE expires_at <= now()
                                               LIMIT {self.EXPIRE_BATCH})
                             RETURNING crop_path, {frame_col}"""
                        )
                        got = cur.fetchall()
                        paths = [r[0] for r in got]
                        # Usually already gone: frames age out after days,
                        # rows after weeks. Handled here for the deployment
                        # that keeps frames as long as rows.
                        frames = [r[1] for r in got if r[1]]
                    if not paths:
                        break
                    out["rows_deleted"] += len(paths)
                    for raw in paths:
                        try:
                            Path(raw).unlink()
                            out["crops_unlinked"] += 1
                        except FileNotFoundError:
                            pass          # already gone; the desired end state
                        except OSError as exc:
                            out["crops_failed"] += 1
                            log.warning("store.expire.unlink_failed path=%s: %s",
                                        raw, exc)
                    for raw in frames:
                        try:
                            Path(raw).unlink()
                            out["frames_unlinked"] += 1
                        except FileNotFoundError:
                            pass
                        except OSError as exc:
                            log.warning("store.expire.frame_unlink_failed path=%s: %s",
                                        raw, exc)
        except Exception as exc:          # noqa: BLE001 — see the docstring
            out["error"] = str(exc).strip().splitlines()[0][:200]
            log.warning("store.expire_failed: %s", out["error"])
        if out["rows_deleted"] or out["error"]:
            log.info("store.expire rows=%d crops=%d failed=%d error=%s",
                     out["rows_deleted"], out["crops_unlinked"],
                     out["crops_failed"], out["error"])
        return out

    # Crops younger than this are never treated as orphans. index/pipeline.py
    # writes the JPEG (line ~234) and inserts the row in a later batch (~264),
    # so a file with no row is the NORMAL state for a moment. Without this grace
    # a background sweep would delete crops belonging to rows about to be
    # written — silently, and only for the busiest cameras. The manual script
    # mostly dodged this by being run when nothing was happening; a thread on a
    # timer has no such luck.
    ORPHAN_GRACE_SECONDS = 3600

    def sweep_orphans(self, crop_root, grace_seconds: int | None = None) -> dict[str, Any]:
        """Delete crop files no row references, and COUNT the reverse. Never raises.

        The other half of unbounded growth, and the one with no row to point at
        it: `write_batch` swallows database failures by design, so a failed batch
        leaves its JPEGs on disk with nothing referencing them, forever. Measured
        on this appliance: 82 such files after two days of normal operation.

        Bounded by mtime rather than by count — see ORPHAN_GRACE_SECONDS.

        THE REVERSE DIRECTION IS REPORTED, NOT REPAIRED. A row whose crop is
        missing or empty is the damage a power cut leaves behind (80 such rows
        here between 2026-09-01 and 09-10, before writer.py made the write
        durable), and unlike a stranded JPEG it is *visible in the product*: the
        hit ranks, and its thumbnail 404s. It is still not deleted here, for two
        reasons. Deleting rows is not this sweep's job — `expire_once` owns
        row-first deletion and its ordering — and a wrong answer here is
        unrecoverable: an unmounted crop volume would make every row look
        dangling, and a sweep that trusted that would empty the index on the one
        boot where the mount failed. So it counts, logs, and leaves
        `scripts/dangling_crops.py` to do the deleting with a human present.

        The counts are the point: this defect had no symptom for nine days.
        """
        grace = self.ORPHAN_GRACE_SECONDS if grace_seconds is None else grace_seconds
        out: dict[str, Any] = {"orphans_removed": 0, "skipped_recent": 0,
                               "failed": 0, "partials_removed": 0,
                               "rows_missing_crop": 0, "rows_empty_crop": 0,
                               "error": None}
        root = Path(crop_root)
        if not root.exists():
            return out
        try:
            crops: set[str] = set()
            frames: set[str] = set()
            with self._get_pool().connection() as conn, conn.cursor() as cur:
                # Frames live under the same root, so they are referenced files
                # too — without them the daily sweep would delete every frame
                # older than the grace period. Kept APART from crops: a frame is
                # expected to vanish before its row (frames are kept days, rows
                # weeks), so counting a missing frame as a dangling crop would
                # raise the power-cut warning every day for a feature working
                # exactly as designed.
                for d in DOMAINS.values():
                    if d.frames:
                        cur.execute(f"SELECT crop_path, frame_path FROM {d.table}")
                        for crop_path, frame_path in cur:
                            crops.add(crop_path)
                            if frame_path:
                                frames.add(frame_path)
                    else:
                        cur.execute(f"SELECT crop_path FROM {d.table}")
                        crops.update(r[0] for r in cur)
            referenced = crops | frames
            cutoff = time.time() - grace
            seen: set[str] = set()
            for f in root.rglob("*.jpg"):
                try:
                    st = f.stat()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    out["failed"] += 1
                    log.warning("store.orphan.stat_failed path=%s: %s", f, exc)
                    continue
                if str(f) in referenced:
                    seen.add(str(f))
                    if st.st_size == 0 and str(f) in crops:
                        out["rows_empty_crop"] += 1
                    continue
                try:
                    if st.st_mtime > cutoff:
                        out["skipped_recent"] += 1
                        continue
                    f.unlink()
                    out["orphans_removed"] += 1
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    out["failed"] += 1
                    log.warning("store.orphan.unlink_failed path=%s: %s", f, exc)
            # Interrupted writes. writer.py encodes into `<uuid>.jpg.part` and
            # renames, so one of these is a write that died between the two —
            # never referenced by a row, and invisible to the `*.jpg` glob above.
            for f in root.rglob("*.jpg" + CROP_PART_SUFFIX):
                try:
                    if f.stat().st_mtime > cutoff:
                        out["skipped_recent"] += 1
                        continue
                    f.unlink()
                    out["partials_removed"] += 1
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    out["failed"] += 1
                    log.warning("store.orphan.part_unlink_failed path=%s: %s", f, exc)
            out["rows_missing_crop"] = len(crops - seen)
        except Exception as exc:  # noqa: BLE001 — background sweep; see expire_once
            out["error"] = str(exc).strip().splitlines()[0][:200]
            log.warning("store.sweep_orphans_failed: %s", out["error"])
        if (out["orphans_removed"] or out["partials_removed"] or out["error"]):
            log.info("store.orphans removed=%d partials=%d skipped_recent=%d "
                     "failed=%d error=%s",
                     out["orphans_removed"], out["partials_removed"],
                     out["skipped_recent"], out["failed"], out["error"])
        if out["rows_missing_crop"] or out["rows_empty_crop"]:
            # WARNING, not info: every one of these is a search hit whose image
            # the operator cannot see, and nothing else in the service says so.
            log.warning("store.dangling_rows missing_crop=%d empty_crop=%d — "
                        "run scripts/dangling_crops.py to inspect",
                        out["rows_missing_crop"], out["rows_empty_crop"])
        return out

    def expire_frames(self, frames_root, keep_days: int) -> dict[str, Any]:
        """Delete whole days of feed frames older than `keep_days`. Never raises.

        Frames live for less time than rows: the feed only shows recent
        detections, and a frame is tens of KB against a crop's ~5. Day
        directories make this a directory removal rather than a query — the
        same layout crops use, so a day can be dealt with without the database.

        A row that outlives its frame keeps frame_path; the endpoint answers
        404 and the UI falls back to the crop. `keep_days <= 0` keeps frames as
        long as their rows, which expire_once then removes with them.
        """
        out: dict[str, Any] = {"frame_days_removed": 0, "frames_removed": 0,
                               "error": None}
        root = Path(frames_root)
        if keep_days <= 0 or not root.exists():
            return out
        # Day directories are named YYYY-MM-DD in UTC — see CropWriter — so a
        # string comparison is a date comparison.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).strftime("%Y-%m-%d")
        try:
            for cam_dir in root.iterdir():
                if not cam_dir.is_dir():
                    continue
                for day_dir in cam_dir.iterdir():
                    name = day_dir.name
                    is_day = (len(name) == 10 and name[4] == "-" and name[7] == "-"
                              and name.replace("-", "").isdigit())
                    if not (day_dir.is_dir() and is_day) or name >= cutoff:
                        continue
                    for f in day_dir.iterdir():
                        try:
                            f.unlink()
                            out["frames_removed"] += 1
                        except FileNotFoundError:
                            pass
                        except OSError as exc:
                            # One stubborn file must not stop this day, or every
                            # older one, from being reclaimed.
                            log.warning("store.expire_frames.unlink_failed path=%s: %s",
                                        f, exc)
                    try:
                        day_dir.rmdir()
                        out["frame_days_removed"] += 1
                    except OSError:
                        pass                    # something left in it; next pass
        except Exception as exc:  # noqa: BLE001 — background sweep; see expire_once
            out["error"] = str(exc).strip().splitlines()[0][:200]
            log.warning("store.expire_frames_failed: %s", out["error"])
        if out["frame_days_removed"] or out["error"]:
            log.info("store.frames days=%d files=%d error=%s",
                     out["frame_days_removed"], out["frames_removed"], out["error"])
        return out

    def health(self) -> dict[str, Any]:
        """Never raises. Returns what it could learn and why, if not much."""
        try:
            with psycopg.connect(self._dsn, connect_timeout=3) as conn, conn.cursor() as cur:
                cur.execute("SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1")
                row = cur.fetchone()
                counts: dict[str, int] = {}
                for name, d in DOMAINS.items():
                    cur.execute(f"SELECT count(*) FROM {d.table}")
                    counts[name] = cur.fetchone()[0]
                persons = counts.get("person", 0)
                vehicles = counts.get("vehicles", 0)
                # Migration 002 is a correctness setting, not a preference: with
                # it off, a scoped search can return zero rows and report
                # success. Surfaced here so a misconfigured database is visible
                # in /health rather than in a support ticket about bad results.
                cur.execute("SHOW hnsw.iterative_scan")
                iterative = cur.fetchone()[0]
            return {
                "reachable": True,
                "schema_version": row[0] if row else None,
                # persons/vehicles keep their names (the VMS reads them);
                # every domain also appears under its own, from the registry.
                "rows": {"persons": persons, "vehicles": vehicles,
                         **{name: n for name, n in counts.items()}},
                "hnsw_iterative_scan": iterative,
                "filtered_search_correct": iterative != "off",
            }
        except psycopg.Error as exc:
            return {"reachable": False, "error": str(exc).strip().splitlines()[0][:200]}
