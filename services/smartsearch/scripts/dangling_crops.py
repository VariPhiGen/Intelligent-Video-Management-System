#!/usr/bin/env python3
"""dangling_crops — find (and optionally delete) rows whose crop is gone or empty.

WHAT THIS CLEANS UP. Until writer.py made the crop write durable, a power cut
could leave a row committed and its JPEG at zero bytes: Postgres fsyncs its
commit, a closed JPEG sits in the page cache for up to 30 seconds. Measured on
this appliance 2026-09-14 — 80 such files between 2026-09-01 and 09-10, in 15
bursts, each burst the last thing written before the service stopped.

WHY THE SWEEP DOES NOT DO THIS. `Store.sweep_orphans` counts these rows and
warns; it will not delete them, because "every row looks dangling" is exactly
what an unmounted crop volume looks like, and a background thread acting on that
would empty the index on the one boot where the mount failed. Here a human is
present, the default is a report, and --apply names the damage it is removing.

A row is deleted, rather than kept with a broken thumbnail, because the pixels
are the evidence: the embedding still matches searches and ranks among real
hits, so keeping it means an operator repeatedly clicking a result that can
never show them anything. For an erasure request the row is worse than useless —
it records that someone was seen without being able to show what was seen.

    python3 scripts/dangling_crops.py                 # report only
    python3 scripts/dangling_crops.py --apply         # delete those rows
    python3 scripts/dangling_crops.py --domain person # one table
    python3 scripts/dangling_crops.py --limit 50      # bound a first run
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from index.config import AppConfig  # noqa: E402

CONFIG = os.environ.get("SEARCH_CONFIG",
                        str(Path(__file__).resolve().parents[1] / "config.yaml"))

# Same two-table enumeration the rest of the store uses. Keep it here rather
# than inlining a table name: a third domain must show up in every sweep, not in
# all but one of them.
TABLES = (("person", "search_persons", "sensor_id"),
          ("vehicles", "search_vehicles", "camera_id"))


def classify(path: str | None) -> str | None:
    """'missing', 'empty', or None when the crop is fine."""
    if not path:
        return "missing"
    try:
        return "empty" if os.path.getsize(path) == 0 else None
    except OSError:
        return "missing"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="delete the rows (default: report only)")
    ap.add_argument("--domain", choices=[d for d, _, _ in TABLES],
                    help="only this domain (default: both)")
    ap.add_argument("--limit", type=int, default=0, help="0 = every row")
    ap.add_argument("--batch", type=int, default=500, help="rows per delete")
    a = ap.parse_args()

    cfg = AppConfig.from_yaml(CONFIG)
    root = Path(cfg.store.crop_dir)
    if not root.exists():
        # The same guard the sweep relies on, and the reason this is a script:
        # an unmounted volume makes every row look dangling.
        print(f"dangling_crops: crop dir {root} does not exist — refusing to "
              f"classify rows against a directory that is not there",
              file=sys.stderr)
        return 2

    total = {"missing": 0, "empty": 0}
    deleted = 0
    with psycopg.connect(cfg.store.dsn) as conn:
        for domain, table, cam_col in TABLES:
            if a.domain and domain != a.domain:
                continue
            sql = f"SELECT id, {cam_col}, ts, crop_path FROM {table} ORDER BY id"
            if a.limit:
                sql += f" LIMIT {a.limit}"
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()

            bad: list[int] = []
            for rid, cam, ts, path in rows:
                kind = classify(path)
                if kind is None:
                    continue
                total[kind] += 1
                bad.append(rid)
                print(f"  {domain:<8} id={rid:<9} {kind:<7} {cam} "
                      f"{ts:%Y-%m-%d %H:%M:%S} {path}")

            print(f"{domain}: {len(bad)} dangling of {len(rows)} row(s) checked")
            if bad and a.apply:
                with conn.cursor() as cur:
                    for i in range(0, len(bad), a.batch):
                        chunk = bad[i:i + a.batch]
                        cur.execute(f"DELETE FROM {table} WHERE id = ANY(%(ids)s)",
                                    {"ids": chunk})
                        deleted += cur.rowcount
                conn.commit()

    print(f"\ndangling_crops: missing={total['missing']} empty={total['empty']}"
          + (f" deleted={deleted}" if a.apply else "  (report only; --apply to delete)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
