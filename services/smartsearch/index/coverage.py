"""coverage.py — keep the index from outliving the footage it describes.

Retention bounds the index by AGE. Nothing bounded it by COVERAGE, and the two
come apart routinely, because the recorder and the indexer are separate services
with separate lifecycles: a restart skew, a camera re-added under a new slug, an
NVR erasure that did not reach here. What is left is a detection standing over
dead air — a hit in Smart Search or on the AI Analytics dashboard whose playback
404s, indistinguishable to an operator from a bug.

THE ONE IMPLEMENTATION, shared by `scripts/purge_unrecorded.py` and by the sweep
thread in `retention.py`. Same rule store.py states for the row-first delete:
two copies of a destructive path is how one of them silently stops holding.

COVERAGE COMES FROM THE NVR, over GET /coverage, not from reading segments.db.
The recorder owns that file, it is SQLite in another container, and a second
reader racing the groomer buys nothing. /coverage also merges boundary jitter,
which a naive reader would report as thousands of one-frame gaps.

Two properties matter more than anything else here, because this runs unattended
on a timer:

  * UNKNOWN IS NOT ABSENT. An unreachable recorder, a 404, a malformed body —
    every one of those would read as "no footage exists" and erase a camera
    whole. `coverage_for` returns None and every caller SKIPS.
  * A PLAUSIBLE ANSWER CAN STILL BE WRONG. A rebuilt or truncated segments.db
    answers successfully and reports almost nothing recorded. `MAX_SWEEP_
    FRACTION` refuses a pass that would take most of a camera's rows, because at
    that point the likelier explanation is that the recorder is confused rather
    than that the footage is genuinely gone. An operator who means it runs the
    script with --force.
"""
from __future__ import annotations

import json
import logging

from .domains import ERASE_KEYS
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

log = logging.getLogger("smartsearch.coverage")

#: Slack on each side of a recorded span before calling a row unrecorded. A crop
#: is stamped with the frame's wall clock and a segment with the muxer's; they
#: drift by well under a second, and erasing a detection one tick outside a
#: segment it plainly belongs to would be the tool doing damage.
EDGE_TOLERANCE_SECONDS = 2.0

#: Sub-second jitter between consecutive segments is not a gap. Same default the
#: NVR's own timeline UI uses.
MIN_GAP_SECONDS = 2.0

#: Rows younger than this are never judged. THE IN-PROGRESS SEGMENT IS NOT A
#: GAP: ffmpeg has not finalised it, so it is not in segments.db and `latest`
#: lags wall-clock by up to one segment duration. The NVR suppresses that
#: trailing window from `gaps` on purpose — to keep a phantom red strip off the
#: timeline — but "after latest segment" is derived here from `latest` itself,
#: so without this grace an hourly sweep would delete the most recent minute of
#: detections on every pass, forever, for footage that exists.
#:
#: Same shape of reasoning as Store.ORPHAN_GRACE_SECONDS: the racing state is
#: NORMAL, not a fault, and the fix is to stop looking at it rather than to
#: reason about it. An hour is far wider than any segment duration, and a row
#: that really is unrecorded is still caught on the next pass.
RECENT_GRACE_SECONDS = 3600.0

#: Refuse a pass that would erase more than this share of one camera's rows.
#: Not a correctness bound — a camera really can lose all its footage — but the
#: difference between that and a recorder answering from a broken index is not
#: visible from here, and only one of the two is recoverable.
MAX_SWEEP_FRACTION = 0.5

TABLES = (("search_persons", "sensor_id"), ("search_vehicles", "camera_id"))


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc)


def indexed_spans(store, camera: Optional[str] = None
                  ) -> dict[str, tuple[float, float, int]]:
    """camera -> (first_ts, last_ts, rows). Only cameras that HAVE rows.

    Driven off the INDEX, not off the registry. A camera removed from the
    registry keeps its rows on purpose (see remove_camera in api/server.py), and
    those are exactly the rows most likely to have outlived their footage — the
    decommissioned camera nobody will think to check.
    """
    spans: dict[str, tuple[float, float, int]] = {}
    for table, cam_col in TABLES:
        sql = (f"SELECT {cam_col} AS cam, extract(epoch from min(ts)) AS lo, "
               f"extract(epoch from max(ts)) AS hi, count(*) AS n FROM {table}")
        params: dict[str, Any] = {}
        if camera:
            sql += f" WHERE {cam_col} = %(camera)s"
            params["camera"] = camera
        for r in store.fetch(sql + f" GROUP BY {cam_col}", params):
            # extract(epoch ...) comes back as Decimal; everything downstream
            # does float arithmetic against NVR epochs.
            lo, hi, n = float(r["lo"]), float(r["hi"]), int(r["n"])
            if r["cam"] in spans:
                p_lo, p_hi, p_n = spans[r["cam"]]
                spans[r["cam"]] = (min(p_lo, lo), max(p_hi, hi), p_n + n)
            else:
                spans[r["cam"]] = (lo, hi, n)
    return spans


def coverage_for(nvr_url: str, camera: str, lo: float, hi: float,
                 timeout: float = 30.0) -> Optional[dict[str, Any]]:
    """What the NVR still holds for this camera. None means UNKNOWN, not empty.

    Every caller skips on None. Refusing to act on an unknown is the only safe
    default for an operation whose action is destruction.
    """
    q = (f"{nvr_url.rstrip('/')}/coverage?camera={urllib.parse.quote(camera)}"
         f"&from={urllib.parse.quote(iso(lo))}&to={urllib.parse.quote(iso(hi))}"
         f"&min_gap_seconds={MIN_GAP_SECONDS}")
    try:
        with urllib.request.urlopen(q, timeout=timeout) as resp:
            body = json.loads(resp.read())
        if not isinstance(body, dict) or "gaps" not in body:
            log.warning("coverage.malformed camera=%s", camera)
            return None
        return body
    except (urllib.error.URLError, urllib.error.HTTPError,
            ValueError, OSError) as exc:
        log.warning("coverage.unavailable camera=%s: %s", camera, exc)
        return None


def unrecorded_windows(cov: dict[str, Any], lo: float,
                       hi: float) -> Iterator[tuple[float, float, str]]:
    """The windows in [lo, hi] with no surviving footage, each with a reason.

    Three kinds are unrecorded and /coverage reports only the first, by design
    ("periods before the camera ever recorded or beyond its latest segment are
    not 'gaps', just unrecorded territory"). It returns earliest/latest too, so
    the other two are computed from the same answer rather than guessed at.

    The reason is carried because the kinds mean different things: a gap in the
    middle is usually an outage worth investigating, while everything-before-
    earliest is normally a camera re-added under a new slug. A tool that erases
    both and reports only a row count hides the first.
    """
    earliest, latest = cov.get("earliest"), cov.get("latest")
    if earliest is None or latest is None:
        # The NVR knows this camera but holds no segment for it at all.
        yield (lo, hi, "no footage at all")
        return
    earliest, latest = float(earliest), float(latest)
    if lo < earliest - EDGE_TOLERANCE_SECONDS:
        yield (lo, min(hi, earliest - EDGE_TOLERANCE_SECONDS),
               "before earliest segment")
    for g in cov.get("gaps") or []:
        g_lo = float(g["start"]) + EDGE_TOLERANCE_SECONDS
        g_hi = float(g["end"]) - EDGE_TOLERANCE_SECONDS
        if g_hi > g_lo:
            yield (max(lo, g_lo), min(hi, g_hi), "recording gap")
    if hi > latest + EDGE_TOLERANCE_SECONDS:
        yield (max(lo, latest + EDGE_TOLERANCE_SECONDS), hi,
               "after latest segment")


def merge_windows(windows: list[tuple[float, float, str]]
                  ) -> list[tuple[float, float, str]]:
    """Coalesce overlapping windows, keeping every reason.

    They CAN overlap: for a camera the recorder still considers live, /coverage
    extends its gap window to wall-clock, so the stretch after the last
    finalised segment arrives both as a reported gap and as "after latest
    segment". Erasing twice is harmless — erase_range is idempotent — but
    COUNTING twice is not: it would report more rows than the index holds, and
    no operator could reconcile the number.
    """
    out: list[tuple[float, float, str]] = []
    for lo, hi, why in sorted(windows):
        if out and lo <= out[-1][1]:
            p_lo, p_hi, p_why = out[-1]
            reasons = p_why if why in p_why.split(" + ") else f"{p_why} + {why}"
            out[-1] = (p_lo, max(p_hi, hi), reasons)
        else:
            out.append((lo, hi, why))
    return out


def count_rows(store, camera: str, start: float, end: float) -> int:
    total = 0
    for table, cam_col in TABLES:
        rows = store.fetch(
            f"SELECT count(*) AS n FROM {table} WHERE {cam_col} = %(c)s "
            f"AND ts >= %(s)s AND ts <= %(e)s",
            {"c": camera, "s": dt(start), "e": dt(end)})
        total += int(rows[0]["n"])
    return total


def plan_for_camera(store, nvr_url: str, camera: str, lo: float, hi: float,
                    rows: int, force: bool = False,
                    now: Optional[float] = None) -> dict[str, Any]:
    """What to erase for one camera, and whether it is safe to. Never raises.

    Returns `windows` only when the whole camera is safe to act on; a refusal
    carries `skipped` with the reason and an empty plan, so a caller cannot
    half-apply one.
    """
    out: dict[str, Any] = {"camera": camera, "windows": [], "rows": 0,
                           "skipped": None, "indexed_rows": rows}
    # The clamp, not a filter on the results: narrowing the WINDOW keeps the
    # recent rows out of the /coverage question entirely, so nothing downstream
    # has to remember to exclude them.
    cutoff = (time.time() if now is None else now) - RECENT_GRACE_SECONDS
    hi = min(hi, cutoff)
    if hi <= lo:
        # Everything this camera has indexed is inside the grace. Not a refusal
        # worth reporting — there is simply nothing old enough to judge yet.
        return out
    cov = coverage_for(nvr_url, camera, lo, hi)
    if cov is None:
        out["skipped"] = "coverage unknown"
        return out
    planned: list[tuple[float, float, str, int]] = []
    total = 0
    for start, end, why in merge_windows(list(unrecorded_windows(cov, lo, hi))):
        n = count_rows(store, camera, start, end)
        if not n:
            continue
        planned.append((start, end, why, n))
        total += n
    if not force and rows and total > rows * MAX_SWEEP_FRACTION:
        # See MAX_SWEEP_FRACTION. A recorder answering from a rebuilt index
        # looks exactly like a camera whose footage is genuinely gone, and only
        # one of those two is recoverable.
        out["skipped"] = (f"would erase {total}/{rows} rows "
                          f"(> {MAX_SWEEP_FRACTION:.0%}) — refusing; "
                          f"check the recorder, then --force if intended")
        return out
    out["windows"], out["rows"] = planned, total
    return out


def purge_unrecorded(store, nvr_url: str, camera: Optional[str] = None,
                     dry_run: bool = False, force: bool = False,
                     now: Optional[float] = None) -> dict[str, Any]:
    """Erase indexed rows with no surviving footage. Never raises.

    Deletion DELEGATES to Store.erase_range — the same call that backs a data
    subject erasure — so the row-first ordering exists in exactly one place.
    Row-first can strand a JPEG at worst, which --sweep-orphans reclaims;
    file-first can leave a searchable hit whose crop 404s, which is the failure
    this is trying to remove.
    """
    result: dict[str, Any] = {
        "cameras_checked": 0, "rows_erased": 0, "crops_unlinked": 0,
        "crops_failed": 0, "skipped": {}, "plans": [], "error": None,
    }
    try:
        spans = indexed_spans(store, camera)
    except Exception as exc:  # noqa: BLE001 — background sweep; see expire_once
        result["error"] = str(exc).strip().splitlines()[0][:200]
        log.warning("coverage.spans_failed: %s", result["error"])
        return result

    for cam in sorted(spans):
        lo, hi, rows = spans[cam]
        result["cameras_checked"] += 1
        try:
            plan = plan_for_camera(store, nvr_url, cam, lo, hi, rows, force, now)
        except Exception as exc:  # noqa: BLE001
            log.warning("coverage.plan_failed camera=%s: %s", cam, exc)
            result["skipped"][cam] = "plan failed"
            continue
        if plan["skipped"]:
            result["skipped"][cam] = plan["skipped"]
            log.warning("coverage.skipped camera=%s: %s", cam, plan["skipped"])
            continue
        result["plans"].append(plan)
        if dry_run:
            result["rows_erased"] += plan["rows"]
            continue
        for start, end, _why, _n in plan["windows"]:
            try:
                r = store.erase_range(cam, dt(start), dt(end))
            except Exception as exc:  # noqa: BLE001 — erase_range RAISES by
                # design (it backs a legal erasure). Here it must not take the
                # sweep thread down with it; the next pass retries the window.
                log.warning("coverage.erase_failed camera=%s: %s", cam, exc)
                result["skipped"][cam] = "erase failed"
                break
            # Every domain's count, from the registry: summing two literals is
            # how a third domain gets erased and not reported. See index/domains.py.
            result["rows_erased"] += sum(int(r.get(k) or 0) for k in ERASE_KEYS)
            result["crops_unlinked"] += r["crops_unlinked"]
            result["crops_failed"] += r["crops_failed"]
    if result["rows_erased"] or result["skipped"]:
        log.info("coverage.sweep cameras=%d rows_erased=%d crops=%d skipped=%s",
                 result["cameras_checked"], result["rows_erased"],
                 result["crops_unlinked"], result["skipped"] or "none")
    return result
