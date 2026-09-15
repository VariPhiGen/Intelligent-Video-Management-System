"""storage/alerts.py — say something BEFORE the disk is full, and say it off-box.

Three warning surfaces already existed and all three require a human to be
looking: the header meter in the SPA, WARNING/CRITICAL lines in the log, and the
Storage tab. An appliance in a cupboard has nobody looking. This module adds the
two that were missing — a computed *state* an operator can be shown, and an
outbound notification for when nobody is watching a screen.

**The cap had no warning at all**, and it is the more insidious of the two:

  * A filling DISK is self-correcting and loud. The engine logs CRITICAL at 95%
    and the disk floor evicts to keep ffmpeg writing.
  * A binding CAP is silent and permanent. Footage is deleted exactly as
    designed, nothing is broken, and the only consequence is that a site
    configured for 30 days is quietly keeping 11 — discovered when someone asks
    for footage from three weeks ago and it is not there.

Binding is read from `RetentionManager.pressure_snapshot()` — events that
actually happened — never inferred from how much footage is on disk. A short
on-disk span means "the cap is winning" only if something evicted, and looks
identical on a camera added yesterday.

PORTABILITY. Everything here runs unchanged on Linux, macOS and Windows:
`shutil.disk_usage` for space (statvfs / GetDiskFreeSpaceExW), `pathlib` for
paths, `time.time()` for clocks, and `urllib.request` from the stdlib for the
webhook — the NVR has no HTTP client dependency and this deliberately does not
add one. Nothing shells out to `df` or `du`, nothing uses `signal.alarm` for
timeouts (SIGALRM does not exist on Windows), and nothing forks. The timeout is
`urlopen(timeout=...)`, which is portable.
"""

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_GB = 1024 ** 3

OK, WARNING, CRITICAL = "ok", "warning", "critical"
_RANK = {OK: 0, WARNING: 1, CRITICAL: 2}

# Disk thresholds match RecordingEngine's DISK_WARN_PCT / DISK_CRIT_PCT so the
# log line, the meter and the notification cannot disagree about what is wrong.
DISK_WARN_PCT = 90.0
DISK_CRIT_PCT = 95.0

# Cap thresholds. Approaching the cap is a WARNING, never critical: nothing is
# failing, the operator is about to start losing retention they think they have.
CAP_WARN_PCT = 90.0
# How long after an eviction the cap still counts as binding. Retention runs
# hourly, so a window shorter than a couple of passes would flicker between
# "binding" and "fine" while the cap evicts on every single pass.
CAP_BINDING_WINDOW_S = 6 * 3600


def evaluate(*, disk_state: dict | None, footage_bytes: int, limit_bytes: int | None,
             pressure: dict, oldest_epoch: float | None,
             configured_days: int | None, now: float | None = None) -> dict:
    """Fold everything into one state plus the reasons behind it. Pure.

    Pure on purpose: this is the whole decision, so it can be tested exhaustively
    without a filesystem, a database or a clock — and identically on every OS.
    """
    now = time.time() if now is None else now
    reasons: list[str] = []
    state = OK

    def raise_to(level: str, why: str) -> None:
        nonlocal state
        reasons.append(why)
        if _RANK[level] > _RANK[state]:
            state = level

    # ── the disk ─────────────────────────────────────────────────────────────
    used_pct = (disk_state or {}).get("used_pct")
    if used_pct is not None:
        if used_pct >= DISK_CRIT_PCT:
            raise_to(CRITICAL, (
                f"Disk {used_pct:.1f}% full — recording stops when the "
                f"filesystem fills; oldest footage is being evicted regardless "
                f"of retention."))
        elif used_pct >= DISK_WARN_PCT:
            raise_to(WARNING, f"Disk {used_pct:.1f}% full.")

    # ── the cap ──────────────────────────────────────────────────────────────
    cap_pct = None
    if limit_bytes:
        cap_pct = 100.0 * footage_bytes / limit_bytes

    last_evict = pressure.get("size_cap_last_at")
    binding = bool(last_evict and (now - last_evict) <= CAP_BINDING_WINDOW_S)

    effective_days = None
    if oldest_epoch:
        effective_days = max(0.0, (now - oldest_epoch) / 86400.0)

    if binding:
        # The message names the shortfall rather than the mechanism, because the
        # mechanism is working correctly and the shortfall is the problem.
        if effective_days is not None and configured_days:
            raise_to(WARNING, (
                f"Storage cap is shortening retention: cameras are configured "
                f"for {configured_days} days but the oldest footage on disk is "
                f"{effective_days:.1f} days old."))
        else:
            raise_to(WARNING, "Storage cap is evicting footage to stay under the limit.")
    elif cap_pct is not None and cap_pct >= CAP_WARN_PCT:
        raise_to(WARNING, (
            f"Storage cap {cap_pct:.0f}% used — the oldest footage starts being "
            f"deleted at 100%, whatever retention says."))

    floor_at = pressure.get("disk_floor_last_at")
    if floor_at and (now - floor_at) <= CAP_BINDING_WINDOW_S:
        raise_to(CRITICAL, (
            "Emergency disk-floor eviction has run — the volume ran out of "
            "space and footage was deleted ignoring retention entirely."))

    return {
        "state": state,
        "reasons": reasons,
        "disk_used_pct": used_pct,
        "cap_usage_pct": (round(cap_pct, 1) if cap_pct is not None else None),
        "cap_binding": binding,
        "effective_retention_days": (round(effective_days, 1)
                                     if effective_days is not None else None),
        "configured_retention_days": configured_days,
        "size_cap_evictions": pressure.get("size_cap_evictions", 0),
        "disk_floor_evictions": pressure.get("disk_floor_evictions", 0),
        "checked_at": now,
    }


class StorageAlerter:
    """Evaluates storage pressure on a timer and notifies on STATE CHANGES.

    On changes, not on every check: at a 60 s cadence a standing critical would
    otherwise generate 1,440 identical notifications a day, and the operator
    would filter the alert away — which is the same as not having one.

    The last notified state is persisted next to the NVR's other small state
    file. Without that, a restart while the disk is full re-fires the alert
    (noise) and a restart while it is *recovering* loses the all-clear (worse:
    the last thing anyone heard was CRITICAL).
    """

    def __init__(self, *, state_path, webhook_url: str = "",
                 interval_seconds: float = 60.0, timeout_seconds: float = 10.0,
                 site: str = "") -> None:
        self._path = Path(state_path)
        self._url = (webhook_url or "").strip()
        self._interval = float(interval_seconds)
        self._timeout = float(timeout_seconds)
        self._site = site
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._current: dict | None = None
        self._last_notified: str | None = self._load()
        self._notify_failures = 0

    # ── persistence ──────────────────────────────────────────────────────────
    def _load(self) -> str | None:
        try:
            data = json.loads(self._path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            logger.warning("StorageAlerter: %s unreadable — starting fresh", self._path)
            return None
        state = data.get("last_notified_state")
        return state if state in _RANK else None

    def _save(self, state: str) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"last_notified_state": state,
                                       "at": time.time()}))
            # os.replace semantics: atomic on POSIX, and on Windows this
            # overwrites rather than raising FileExistsError as rename would.
            tmp.replace(self._path)
        except OSError as exc:
            logger.warning("StorageAlerter: cannot persist state: %s", exc)

    # ── evaluation ───────────────────────────────────────────────────────────
    def check(self, snapshot: dict) -> dict:
        """Evaluate one already-gathered snapshot, notifying on a transition."""
        result = evaluate(**snapshot)
        with self._lock:
            self._current = result
            previous = self._last_notified
        if result["state"] != previous:
            self._notify(result, previous)
            with self._lock:
                self._last_notified = result["state"]
            self._save(result["state"])
        return result

    # ── notification ─────────────────────────────────────────────────────────
    def _notify(self, result: dict, previous: str | None) -> None:
        level = result["state"]
        summary = "; ".join(result["reasons"]) or "Storage pressure cleared."
        # Always logged, webhook or not: a deployment with no webhook still gets
        # the transition in the log, which is where the first three surfaces
        # already are.
        log = logger.critical if level == CRITICAL else (
            logger.warning if level == WARNING else logger.info)
        log("STORAGE %s (was %s): %s", level.upper(), previous or "unknown", summary)
        if not self._url:
            return

        payload = {
            "source": "vms-nvr",
            "site": self._site,
            "event": "storage.pressure",
            "state": level,
            "previous_state": previous,
            "summary": summary,
            "reasons": result["reasons"],
            "disk_used_pct": result["disk_used_pct"],
            "cap_usage_pct": result["cap_usage_pct"],
            "cap_binding": result["cap_binding"],
            "effective_retention_days": result["effective_retention_days"],
            "configured_retention_days": result["configured_retention_days"],
            "at": result["checked_at"],
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "vms-nvr-storage-alerter"},
        )
        try:
            # urlopen's own timeout — portable. signal.alarm would not be:
            # SIGALRM does not exist on Windows and only works on the main
            # thread on POSIX, and this runs in a worker.
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                if resp.status >= 300:
                    raise urllib.error.HTTPError(
                        self._url, resp.status, "unexpected status", resp.headers, None)
            self._notify_failures = 0
            logger.info("StorageAlerter: notified %s of state %s", self._url, level)
        except Exception as exc:  # noqa: BLE001 — never let alerting break the NVR
            self._notify_failures += 1
            logger.warning("StorageAlerter: webhook POST failed (%s): %s",
                           self._notify_failures, exc)
            # Deliberately NOT retried and NOT rolled back. The state is still
            # recorded as notified, so a flapping endpoint cannot turn one
            # transition into an unbounded retry loop; the next real transition
            # notifies again, and the state is on /storage regardless.

    # ── thread ───────────────────────────────────────────────────────────────
    def start(self, gather) -> None:
        """`gather` returns the kwargs for evaluate(); called on the timer."""
        if self._thread is not None or self._interval <= 0:
            if self._interval <= 0:
                logger.warning("Storage alerting disabled (interval=%s)", self._interval)
            return
        def run() -> None:
            while not self._stop.wait(self._interval):
                try:
                    self.check(gather())
                except Exception:  # noqa: BLE001
                    logger.exception("Storage alert check failed — continuing")
        self._thread = threading.Thread(target=run, name="storage-alerts", daemon=True)
        self._thread.start()
        logger.info("Storage alerting every %.0fs%s", self._interval,
                    f", webhook -> {self._url}" if self._url else " (log only)")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def snapshot(self) -> dict | None:
        with self._lock:
            return dict(self._current) if self._current else None
