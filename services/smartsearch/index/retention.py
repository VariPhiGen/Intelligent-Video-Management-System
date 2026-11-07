"""retention.py — the timer that keeps the index bounded.

`store.write_batch` stamps every row with `expires_at` from `retention_minutes`,
and until this existed **nothing ever acted on it**. There was a sweep —
`scripts/retention.py` — but no cron entry, no compose sidecar and no thread
called it, so `retention_minutes: 43200` was policy on paper: rows and their JPEGs
accumulated forever, at ~5.7 kB per row plus the crop, on a service whose own
plan named storage as the binding constraint.

It had not bitten yet only because the index is younger than its own retention
window — first expiry on this appliance was 28 days out when this was written.
That is the whole hazard: a defect with a start date, on a service that looks
healthy right up until the day it doesn't.

The sweep lives here rather than in a sidecar because this process already owns
both halves of the job — the connection pool and the crop directory — and a
sidecar would need its own copy of the DSN, the crop path, and the row-first
ordering. The actual deletion is `Store.expire_once`, shared with the script, so
there is exactly one implementation of that ordering.

It sweeps three things now, composed into one pass. Expiry drops what aged out.
The COVERAGE sweep (index/coverage.py) drops what lost its footage — the drift
that age-based expiry cannot see, because the recorder and the indexer are
separate services that restart independently, and every skew leaves detections
standing over dead air whose playback 404s. The orphan sweep then reclaims any
JPEG the other two stranded. The coverage sweep was a script before it was here,
which meant it corrected drift only when someone remembered it existed, while
the drift itself accrued at every restart.

Deliberately NOT tied to model lifecycle. Hibernation releases the detector and
encoder when no camera is registered; expiry has to keep running regardless, or
switching Smart Search off on every camera would freeze the index at its current
size forever — the state in which unbounded growth is least visible.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

log = logging.getLogger("smartsearch.retention")


class RetentionThread:
    """Daemon thread that calls ``Store.expire_once`` on an interval."""

    def __init__(self, store, interval_seconds: float = 3600.0,
                 initial_delay_seconds: float = 60.0,
                 crop_root: str | None = None,
                 orphan_every_n: int = 24,
                 nvr_url: str | None = None,
                 coverage_every_n: int = 24,
                 frames_root: str | None = None,
                 frame_retention_days: int = 7) -> None:
        self._store = store
        # Whole frames for the feed age out by DAY DIRECTORY, every pass — it
        # lists a handful of directories, so there is nothing to save by doing
        # it daily. See Store.expire_frames.
        self._frames_root = frames_root
        self._frame_days = int(frame_retention_days)
        # The OTHER way the index outlives its footage, and the one age-based
        # expiry cannot see: the recorder and the indexer are separate services,
        # so a restart skew, a camera re-added under a new slug, or an NVR
        # erasure that did not reach here leaves detections standing over dead
        # air. Left to a script nobody remembers to run, that drift is permanent
        # and grows with every restart — which is why it belongs on the same
        # timer as expiry rather than in the runbook.
        #
        # Every 24th pass, like the orphan sweep and for the same reason: it
        # costs an HTTP round trip per camera, and the drift it corrects appears
        # at restarts, not continuously. 0 disables it; so does no NVR URL,
        # since coverage is the only thing that makes it safe.
        self._nvr_url = (nvr_url or "").strip() or None
        self._coverage_every_n = int(coverage_every_n)
        # Which pass last reconciled successfully. None means never, and that is
        # why the FIRST pass sweeps rather than the 24th: a restart is precisely
        # when this drift is created, so waiting a day to look guarantees a day
        # of hits whose playback 404s.
        #
        # It stays None while the recorder cannot be reached, so a boot where
        # the NVR is not up yet retries on the next hourly pass instead of
        # standing down for a full cycle. "Could not ask" must not be recorded
        # as "asked and found nothing" — the same distinction coverage.py draws
        # to decide whether it may delete at all.
        self._coverage_last_ok: int | None = None
        # Orphan sweeping walks the whole crop tree and reads every crop_path in
        # the database, so it is not worth doing hourly. Every 24th pass — daily
        # at the default interval — keeps the residue bounded without making the
        # common sweep expensive. 0 disables it.
        self._crop_root = crop_root
        self._orphan_every_n = int(orphan_every_n)
        # Ceiling, not the interval itself. Expiry is stamped from each
        # camera's RECORDING retention, which can now be minutes, and an
        # hourly sweep would leave a twenty-minute camera's crops searchable
        # for up to eighty — the index outliving the footage by four times the
        # policy, which is the exact failure this module exists to prevent.
        # `_sweep_interval` divides the shortest retention on the appliance by
        # SWEEP_DIVISOR and clamps it into [MIN_INTERVAL, _max_interval],
        # mirroring the recorder's own pacing (nvr/storage/retention.py) so the
        # two services delete on comparable clocks.
        self._max_interval = float(interval_seconds)
        # Not at t=0: boot is when the database is least likely to be up and
        # most likely to be busy, and a sweep that fails there just logs an
        # error into the first page of the service's life. A minute in, the
        # pool is warm and /health has already answered.
        self._initial_delay = float(initial_delay_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last: dict[str, Any] | None = None
        self._sweeps = 0

    #: Sweep at least this many times per retention period, bounding how long
    #: an expired row can outlive its deadline at 1/4 of the period. Matches
    #: the recorder's RetentionManager.SWEEP_DIVISOR.
    SWEEP_DIVISOR = 4
    #: Never sweep more often than this, whatever the retention. A sweep is a
    #: DELETE over every domain table; a crop cannot be finer-grained than the
    #: minute of footage it came from anyway.
    MIN_INTERVAL_S = 60.0

    @property
    def enabled(self) -> bool:
        """0 or negative disables the sweep — for an operator who runs the
        script from cron instead, or is debugging what a sweep deleted."""
        return self._max_interval > 0

    def _sweep_interval(self) -> float:
        """How long to wait before the next sweep.

        Derived after every pass rather than once at startup: the registry
        pushes retention changes at runtime, so a camera dropped to twenty
        minutes has to pull the sweep in without a restart. A fleet on the
        default thirty days gets the configured interval exactly as before.
        """
        try:
            shortest = self._store.shortest_retention_minutes()
        except Exception:  # noqa: BLE001 — pacing must never kill the thread
            log.exception("Retention: could not read shortest retention")
            return self._max_interval
        if not shortest or shortest <= 0:
            return self._max_interval
        want = (shortest * 60.0) / self.SWEEP_DIVISOR
        return min(self._max_interval, max(self.MIN_INTERVAL_S, want))

    def start(self) -> None:
        if not self.enabled:
            log.warning(
                "Retention sweep DISABLED (interval=%s). Expired rows and crop "
                "files will accumulate until scripts/retention.py is run by hand.",
                self._max_interval)
            return
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="retention-sweep", daemon=True)
        self._thread.start()
        log.info("Retention sweep at most every %.0fs (first in %.0fs); the "
                 "actual cadence follows the shortest camera retention",
                 self._max_interval, self._initial_delay)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def orphan_sweep_enabled(self) -> bool:
        return bool(self._crop_root) and self._orphan_every_n > 0

    @property
    def coverage_sweep_enabled(self) -> bool:
        return bool(self._nvr_url) and self._coverage_every_n > 0

    def sweep_now(self) -> dict[str, Any]:
        """Run one sweep synchronously. Used by the thread and by tests."""
        result = self._store.expire_once()
        with self._lock:
            self._sweeps += 1
            due = (self.orphan_sweep_enabled
                   and self._sweeps % self._orphan_every_n == 0)
            coverage_due = self.coverage_sweep_enabled and (
                self._coverage_last_ok is None
                or self._sweeps - self._coverage_last_ok >= self._coverage_every_n)
        # BEFORE the orphan sweep and after expiry, so the three compose into
        # one pass: expiry drops what aged out, this drops what lost its
        # footage, and the orphan sweep reclaims any JPEG either of them
        # stranded — instead of leaving that residue for a day.
        #
        # force=False always. The safety brake exists precisely for the
        # unattended caller; an operator who has looked at the recorder and
        # means it runs scripts/purge_unrecorded.py --force.
        if coverage_due:
            from . import coverage as coverage_mod
            swept = coverage_mod.purge_unrecorded(
                self._store, self._nvr_url, force=False)
            result = {**result, "coverage": swept}
            # Only a pass that actually learned what the recorder holds counts
            # as done. A refusal for any other reason — the safety brake, a
            # failed erase — is a decision that WAS reached, and repeating it
            # hourly would just log the same complaint 24 times a day.
            unknown = any(why == "coverage unknown"
                          for why in swept["skipped"].values())
            if not (unknown or swept["error"]):
                with self._lock:
                    self._coverage_last_ok = self._sweeps
        if self._frames_root and self._frame_days > 0:
            result = {**result, "frames": self._store.expire_frames(
                self._frames_root, self._frame_days)}
        if due:
            # Deliberately after expiry, not before: expiry deletes rows and
            # unlinks their files, and anything it failed to unlink is an orphan
            # by the time this runs — so the two compose into one pass instead of
            # leaving the residue for a day.
            result = {**result, "orphans": self._store.sweep_orphans(self._crop_root)}
        with self._lock:
            self._last = {**result, "at": time.time()}
        return result

    def _run(self) -> None:
        # wait() rather than sleep() so shutdown is immediate instead of taking
        # up to a full interval — a container stop should not wait an hour.
        if self._stop.wait(self._initial_delay):
            return
        while not self._stop.is_set():
            try:
                self.sweep_now()
            except Exception:  # noqa: BLE001
                # expire_once already swallows its own errors; this is the belt
                # for anything else, because a thread that dies takes the only
                # bound on index growth with it and says nothing.
                log.exception("Retention sweep raised — thread continuing")
            self._stop.wait(self._sweep_interval())

    def snapshot(self) -> dict[str, Any]:
        """Lock-free-ish view for /health. Never blocks on a running sweep."""
        with self._lock:
            last, sweeps = self._last, self._sweeps
        return {
            "enabled": self.enabled,
            # What the NEXT wait will be, not the configured ceiling: on an
            # appliance with a minute-scale camera those differ by an order of
            # magnitude, and /health reporting the ceiling would describe a
            # cadence the service is not running on.
            "interval_seconds": (self._sweep_interval() if self.enabled else None),
            "max_interval_seconds": self._max_interval if self.enabled else None,
            "orphan_sweep_every_n": (self._orphan_every_n
                                     if self.orphan_sweep_enabled else None),
            # None means nothing is reconciling the index against the
            # recordings — the state in which a hit whose playback 404s is
            # permanent. Reported for the same reason `enabled` is.
            "coverage_sweep_every_n": (self._coverage_every_n
                                       if self.coverage_sweep_enabled else None),
            # How long whole frames for the feed are kept; None when nothing
            # is ageing them out (0, or no frames directory configured).
            "frame_retention_days": (self._frame_days
                                     if self._frames_root and self._frame_days > 0
                                     else None),
            "sweeps": sweeps,
            # None until the first sweep completes — distinguishable from a
            # sweep that ran and deleted nothing, which reports zeroes.
            "last": last,
        }
