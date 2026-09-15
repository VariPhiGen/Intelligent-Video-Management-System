"""writer.py — crop JPEGs on disk, rows in the database.

Crops never live in a row. A JPEG in a column is a column nobody can vacuum, and
the retention sweep already has to touch the filesystem anyway.

Layout is date-partitioned per camera:

    <crop_dir>/<domain>/<slug>/<YYYY-MM-DD>/<uuid>.jpg
    <crop_dir>/frames/<slug>/<YYYY-MM-DD>/<ts_ms>.jpg     whole frames, for the feed

so that a day's crops can be found, counted or removed without consulting the
database — which matters when the database is the thing that went wrong.

WHY THE WRITE IS ATOMIC AND DURABLE, which a plain `crop.save(path)` is not.
Measured on this appliance 2026-09-14: **80 zero-byte crop JPEGs** across
2026-09-01 → 09-10, in 15 bursts, every burst the last thing written before the
service stopped for minutes or hours. Rows in the database pointed at them, so
they were not failed writes — `save()` had returned a path and the row was
committed.

That is the signature of the two stores having different durability. Postgres
fsyncs its commit; a JPEG written and closed sits in the page cache for up to
30 seconds. Lose the machine in that window and the row survives while its
pixels do not, which is the one combination nothing downstream can detect: the
row looks indexable, searches match it, and the thumbnail is an empty 200.

So the contract here is: **a path this function returns has its bytes on disk.**
Encode into `<uuid>.jpg.part`, fsync it, then `os.replace()` into place. A crash
can now leave a `.part` file (an orphan, which sweep_orphans reclaims) or
nothing at all — never a referenced empty file. The rename is deliberately NOT
followed by a directory fsync: losing the rename loses the name, which is the
orphan case, and that one is already swept.

The cost was measured before it was chosen, 200 crops on this box's NVMe:

    direct save          0.53 ms/crop
    tmp+replace          0.54 ms/crop
    tmp+fsync+replace    5.61 ms/crop

~5 ms of that is the fsync, and it is paid on the ingest request thread. At this
site's rate (~6k crops/day, bursts of a few per second) it is noise; a site
running an order of magnitude more can turn it off with `SEARCH_CROP_FSYNC=0`
and get atomicity without durability — a torn write still cannot be referenced,
but a power cut can still empty a referenced file.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

log = logging.getLogger("smartsearch.writer")

JPEG_QUALITY = 82          # visibly fine for a thumbnail; ~40% of quality 95
#: Beside the domain directories, never inside one. No domain is named `frames`
#: (see index/domains.py), so the sibling cannot collide with one.
FRAMES_DIR = "frames"

#: Suffix for the half-written file. NOT matched by the `*.jpg` glob the orphan
#: sweep walks, so an interrupted write cannot be mistaken for a crop; the sweep
#: collects these separately once they are older than its grace window.
PART_SUFFIX = ".part"


class CropWriter:
    def __init__(self, crop_dir: str, fsync: bool = True) -> None:
        self._root = Path(crop_dir)
        self._fsync = fsync
        self.write_failures = 0

    @property
    def frames_root(self) -> Path:
        return self._root / FRAMES_DIR

    def save(self, domain: str, slug: str, crop: Image.Image, ts: float) -> str | None:
        """Write one crop and return its path, or None if it could not be
        written. None is a skip, not a crash: a full disk should cost the index
        rows, not the service.

        A returned path is a promise that the file is complete — the caller
        writes a row against it, and a row whose crop is empty is worse than no
        row at all, because only the row is visible to a search.
        """
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        directory = self._root / domain / slug / day
        path = directory / f"{uuid.uuid4().hex}.jpg"
        part = path.with_name(path.name + PART_SUFFIX)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with open(part, "wb") as fh:
                crop.save(fh, format="JPEG", quality=JPEG_QUALITY, optimize=True)
                fh.flush()
                if self._fsync:
                    os.fsync(fh.fileno())
            # Belt and braces against the failure this whole function exists to
            # prevent: an encoder that writes nothing and raises nothing would
            # otherwise hand back a path to an empty file, which is exactly the
            # state we are trying to make unreachable.
            if part.stat().st_size == 0:
                raise OSError("encoder produced an empty file")
            os.replace(part, path)
            return str(path)
        except (OSError, ValueError) as exc:
            self.write_failures += 1
            log.warning("crop write failed for %s/%s: %s", domain, slug, exc)
            self._discard(part)
            return None

    def save_frame(self, slug: str, ts: float, jpeg: bytes) -> str | None:
        """Write the whole frame an observation came from; return its path.

        ONE FILE PER DECODED FRAME, not per object. The name is the frame's own
        timestamp in milliseconds, so the second and third object found in the
        same frame land on the file the first one wrote and reuse it — three
        rows, one image. The bytes are already JPEG (analytics encoded them
        once), so they are written as they are, not re-encoded.

        None is a skip, as for crops: a full disk costs the feed its picture,
        not the row. A slug that could climb out of the frames directory is
        refused rather than written.

        SAME CONTRACT AS save(): a returned path has its bytes on disk. A row
        references this file and Postgres fsyncs that row, so an unflushed frame
        is the referenced-empty-file hazard the module docstring measured for
        crops. `SEARCH_CROP_FSYNC=0` relaxes both together. The half-written
        name is unique per call, because two objects from one frame are written
        concurrently and must not truncate each other's temp file.
        """
        if not slug or slug in (".", "..") or "/" in slug or "\\" in slug:
            log.warning("frame refused for unsafe camera slug %r", slug)
            return None
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        directory = self.frames_root / slug / day
        stem = str(int(round(ts * 1000)))
        path = directory / f"{stem}.jpg"
        # "<ts>.<uuid>.jpg.part" — still matched by the sweep's *.jpg.part glob.
        part = directory / f"{stem}.{uuid.uuid4().hex}.jpg{PART_SUFFIX}"
        try:
            if path.exists():
                return str(path)
            directory.mkdir(parents=True, exist_ok=True)
            with open(part, "wb") as fh:
                fh.write(jpeg)
                fh.flush()
                if self._fsync:
                    os.fsync(fh.fileno())
            os.replace(part, path)
            return str(path)
        except OSError as exc:
            self.write_failures += 1
            log.warning("frame write failed for %s: %s", slug, exc)
            self._discard(part)
            return None

    @staticmethod
    def _discard(part: Path) -> None:
        """Remove the half-written file. Failing to is not worth an exception —
        the orphan sweep collects stale `.part` files for exactly this case."""
        try:
            part.unlink()
        except OSError:
            pass
