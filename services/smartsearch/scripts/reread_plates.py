#!/usr/bin/env python3
"""reread_plates — re-run plate recognition over crops already indexed.

Fixing the reader does not fix history. The crops are still on disk, so a row
read before a recogniser change can be read again — which matters because a
truncated plate is not merely imprecise, it is UNFINDABLE: plate search is
`ILIKE '%needle%'`, so an operator who types the correct full plate matches
nothing against a stored value that is shorter.

ONLY LONGER READS ARE APPLIED, and that is not conservatism for its own sake.
Ingest reads the plate from the RAW FRAME; what is on disk is a JPEG at quality
82. So a re-read is a read of a lossy copy, and on a marginal plate it can lose
as much as it gains — measured across 489 rows: 128 got longer, 127 changed at
the same length, and **19 got shorter**. Same-length changes are compression
noise rather than evidence of anything, and shorter is a straight regression.
Recovered characters are the signature of the defect being fixed, so that is the
only change worth writing back. `--allow-any-change` lifts it, for a run against
crops you know are clean.

A crop whose file has been retained away is skipped, not blanked: no read is not
evidence of no plate.

    python3 scripts/reread_plates.py --dry-run          # report, change nothing
    python3 scripts/reread_plates.py                    # apply
    python3 scripts/reread_plates.py --plate JH10AF793  # one plate only
    python3 scripts/reread_plates.py --limit 500        # bound a first run
    python3 scripts/reread_plates.py --allow-any-change # also write non-longer reads
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import psycopg
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from index import plates as plates_mod  # noqa: E402
from index.config import AppConfig  # noqa: E402

CONFIG = os.environ.get("SEARCH_CONFIG", str(Path(__file__).resolve().parents[1] / "config.yaml"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--plate", help="only rows whose stored plate matches this (substring)")
    ap.add_argument("--limit", type=int, default=0, help="0 = every row with a plate")
    ap.add_argument("--batch", type=int, default=200, help="rows per commit")
    ap.add_argument("--allow-any-change", action="store_true",
                    help="write same-length and shorter re-reads too (see the module docstring)")
    a = ap.parse_args()

    cfg = AppConfig.from_yaml(CONFIG)
    reader = plates_mod.build(cfg.plates, None)
    if reader is None or not reader.active:
        print("reread: plate reading is not active (no localiser weights) — nothing to do",
              file=sys.stderr)
        return 2

    where = "plate IS NOT NULL"
    params: dict = {}
    if a.plate:
        where += " AND plate ILIKE %(p)s"
        params["p"] = f"%{a.plate.upper()}%"
    sql = f"SELECT id, crop_path, plate, plate_confidence FROM search_vehicles WHERE {where} ORDER BY id"
    if a.limit:
        sql += f" LIMIT {a.limit}"

    seen = missing = unchanged = changed = lost = skipped_not_longer = 0
    pending: list[tuple[str, float, int]] = []

    with psycopg.connect(cfg.store.dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        print(f"reread: {len(rows)} row(s) to check")

        for rid, path, old_plate, _old_conf in rows:
            seen += 1
            if not path or not os.path.exists(path):
                # The row outlived its crop. Leaving the plate as-is is the
                # honest outcome: it was read from a frame that existed.
                missing += 1
                continue
            try:
                crop = np.array(Image.open(path).convert("RGB"))
            except (OSError, ValueError) as exc:
                print(f"  unreadable {path}: {exc}", file=sys.stderr)
                missing += 1
                continue

            got = reader.read(crop)
            if got is None:
                # The new reader found nothing where the old one found a plate.
                # Never blank the row on that: an absent read is not evidence
                # that the vehicle had no plate, and deleting a searchable value
                # is the one change that cannot be undone from here.
                lost += 1
                continue
            if got.text == old_plate:
                unchanged += 1
                continue
            if not a.allow_any_change and len(got.text) <= len(old_plate):
                # Not a recovered truncation. Re-reading a JPEG of a marginal
                # plate produces a different guess about as often as a better
                # one, and overwriting a stored read with a coin flip is not a
                # fix — it just moves the error.
                skipped_not_longer += 1
                continue

            changed += 1
            print(f"  {rid}: {old_plate} -> {got.text}  ({got.confidence:.3f})")
            pending.append((got.text, float(got.confidence), rid))

            if not a.dry_run and len(pending) >= a.batch:
                _flush(conn, pending)
                pending.clear()

        if not a.dry_run and pending:
            _flush(conn, pending)

    verb = "would change" if a.dry_run else "changed"
    print(f"\nreread: {seen} checked · {verb} {changed} · unchanged {unchanged} · "
          f"not longer, skipped {skipped_not_longer} · crop missing {missing} · "
          f"no read now {lost}")
    print(f"        two-line recoveries this run: {reader.snapshot()['two_line_reads']}")
    return 0


def _flush(conn, pending) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE search_vehicles SET plate = %s, plate_confidence = %s WHERE id = %s",
            pending,
        )
    conn.commit()


if __name__ == "__main__":
    raise SystemExit(main())
