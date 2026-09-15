#!/usr/bin/env python3
"""retention — drop crops whose searchable life has expired.

The service now sweeps on its own timer (index/retention.py), so this script is
no longer the only thing standing between the index and unbounded growth — which
it was, unscheduled, until that thread existed. It remains useful for a sweep on
demand, for --dry-run before changing retention_days, and for --sweep-orphans,
which the timed sweep does not do.

The expiry pass DELEGATES to Store.expire_once rather than keeping its own copy.
There used to be one implementation here and none in the service; having one in
each is how the row-first ordering below silently stops holding in one of them.

ORDER MATTERS, and it is row-first on purpose. Deleting the file first and then
failing to delete the row leaves a hit that 404s when the operator clicks it —
the index says the footage is there and it is not. Row-first can only leave an
orphaned JPEG, which costs disk and nothing else, and --sweep-orphans reclaims
it. Prefer the failure nobody can see in the product.

    python3 scripts/retention.py                # delete what has expired
    python3 scripts/retention.py --dry-run      # count only, change nothing
    python3 scripts/retention.py --sweep-orphans  # crop files no row references
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from index.store import Store  # noqa: E402

DSN = os.environ.get(
    "SEARCHDB_URL", "postgresql://search:search_secret@127.0.0.1:5434/smartsearch"
)
CROP_ROOT = Path(os.environ.get("SEARCH_CROP_DIR", "/data/search/crops"))
TABLES = ("search_persons", "search_vehicles")


def expire(conn, dry: bool) -> tuple[int, int]:
    """Count what has expired (dry run), or delegate the delete to the store.

    The dry run stays here because it only counts and needs no pool; the real
    delete goes through Store.expire_once so this script and the service share
    one implementation of the row-first ordering.
    """
    if dry:
        rows = 0
        for table in TABLES:
            with conn.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM {table} WHERE expires_at <= now()")
                n = cur.fetchone()[0]
            print(f"  {table}: {n} row(s) expired")
            rows += n
        return rows, 0

    store = Store(DSN)
    try:
        result = store.expire_once()
    finally:
        store.close()
    if result.get("error"):
        print(f"  retention.failed: {result['error']}", file=sys.stderr)
    if result.get("crops_failed"):
        print(f"  {result['crops_failed']} crop file(s) could not be unlinked "
              f"— run --sweep-orphans", file=sys.stderr)
    return result["rows_deleted"], result["crops_unlinked"]


def sweep_orphans(conn, dry: bool) -> int:
    """Crop files on disk that no row references — the residue of a row-first
    delete that could not unlink, of a write_batch whose insert failed after the
    JPEG was written, or of a database restored from backup.

    Delegates to Store.sweep_orphans for the same reason expire() delegates.
    The script passes grace_seconds=0: it is run by a human who can see whether
    ingest is busy, whereas the in-service sweep keeps the default grace because
    crops are written before their rows and it has no such judgement.
    """
    if not CROP_ROOT.exists():
        print(f"  crop dir {CROP_ROOT} does not exist — nothing to sweep")
        return 0
    if dry:
        referenced: set[str] = set()
        with conn.cursor() as cur:
            for table in TABLES:
                cur.execute(f"SELECT crop_path FROM {table}")
                referenced.update(r[0] for r in cur)
        n = sum(1 for f in CROP_ROOT.rglob("*.jpg") if str(f) not in referenced)
        print(f"  {n} orphaned crop file(s) (dry run)")
        return n

    store = Store(DSN)
    try:
        result = store.sweep_orphans(CROP_ROOT, grace_seconds=0)
    finally:
        store.close()
    if result.get("error"):
        print(f"  retention.failed: {result['error']}", file=sys.stderr)
    if result.get("failed"):
        print(f"  {result['failed']} file(s) could not be unlinked", file=sys.stderr)
    print(f"  {result['orphans_removed']} orphaned crop file(s) removed")
    return result["orphans_removed"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sweep-orphans", action="store_true")
    a = ap.parse_args()
    try:
        conn = psycopg.connect(DSN)
    except psycopg.Error as exc:
        print(f"retention: cannot connect (check SEARCHDB_URL): {exc}", file=sys.stderr)
        return 2
    with conn:
        if a.sweep_orphans:
            sweep_orphans(conn, a.dry_run)
        else:
            rows, files = expire(conn, a.dry_run)
            verb = "would delete" if a.dry_run else "deleted"
            print(f"retention.ok  {verb} {rows} row(s), {files} crop file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
