#!/usr/bin/env python3
"""purge_unrecorded — drop indexed crops whose footage no longer exists.

The service now sweeps for this on its own timer (index/retention.py), the same
way it does for expiry, so this script is no longer the only thing keeping the
index and the recordings in step. It remains useful for a sweep on demand, for
--dry-run before trusting the timer, and for --force, which the unattended sweep
deliberately has no way to ask for.

The logic lives in index/coverage.py and is shared with that thread. Two copies
of a destructive path is how one of them silently stops holding — the same rule
store.py states for the row-first delete.

    python3 scripts/purge_unrecorded.py --dry-run    # report, change nothing
    python3 scripts/purge_unrecorded.py              # erase
    python3 scripts/purge_unrecorded.py --camera X   # one camera
    python3 scripts/purge_unrecorded.py --force      # override the safety brake

--force overrides MAX_SWEEP_FRACTION, which refuses a pass that would take most
of a camera's rows. Read the refusal first: a recorder answering from a rebuilt
or truncated segments.db looks exactly like a camera whose footage is genuinely
gone, and only one of those two is recoverable.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from index import coverage as cov  # noqa: E402
from index.store import Store  # noqa: E402

DSN = os.environ.get(
    "SEARCHDB_URL", "postgresql://search:search_secret@127.0.0.1:5434/smartsearch"
)
NVR_URL = os.environ.get("NVR_URL", "http://127.0.0.1:8009")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be erased, change nothing")
    ap.add_argument("--camera", default=None, help="limit to one camera slug")
    ap.add_argument("--force", action="store_true",
                    help="erase even when a camera exceeds the safety brake")
    a = ap.parse_args()

    # The module logs its refusals at WARNING; an operator running this by hand
    # needs to see them, not have them swallowed by an unconfigured root logger.
    logging.basicConfig(level=logging.INFO, format="  %(message)s",
                        stream=sys.stderr)

    store = Store(DSN)
    try:
        res = cov.purge_unrecorded(store, NVR_URL, camera=a.camera,
                                   dry_run=a.dry_run, force=a.force)
    finally:
        store.close()

    if res["error"]:
        print(f"purge_unrecorded: {res['error']}", file=sys.stderr)
        return 2
    for plan in res["plans"]:
        print(f"{plan['camera']}  ({plan['indexed_rows']} indexed rows)")
        for start, end, why, n in plan["windows"]:
            print(f"    {cov.iso(start)[11:19]}-{cov.iso(end)[11:19]} "
                  f"{(end - start) / 60.0:7.1f} min  {n:5d} rows  ({why})")
    verb = "would erase" if a.dry_run else "erased"
    print(f"\npurge_unrecorded.ok  checked {res['cameras_checked']} camera(s), "
          f"{verb} {res['rows_erased']} row(s)"
          + ("" if a.dry_run else f", {res['crops_unlinked']} crop file(s)"))
    if res["crops_failed"]:
        # Rows gone, images not: an INCOMPLETE erasure, and the outcome most
        # worth a non-zero exit. --sweep-orphans reclaims the files.
        print(f"purge_unrecorded: {res['crops_failed']} crop file(s) could not "
              f"be unlinked — run retention.py --sweep-orphans", file=sys.stderr)
        return 1
    if res["skipped"]:
        for cam, why in sorted(res["skipped"].items()):
            print(f"purge_unrecorded: skipped {cam}: {why}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
