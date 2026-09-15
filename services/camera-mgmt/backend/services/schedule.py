"""schedule.py — evaluate a camera's recording schedule.

Schedule shape (cameras.recording_schedule JSONB, validated at the API by
RecordingSchedule in models.py):

  None / {}  → record 24/7 (default)
  {"mode": "weekly",  "rules": [{"days": [0..6],  "start": "HH:MM", "end": "HH:MM"}]}
  {"mode": "monthly", "rules": [{"days": [1..31], "start": "HH:MM", "end": "HH:MM"}]}

weekly days are ISO weekdays (0=Monday … 6=Sunday); monthly days are days of
the month. Times are SERVER-LOCAL wall clock (the TZ env var — same clock the
NVR stamps segment filenames with). "24:00" is a valid end meaning end-of-day.
A rule whose end <= start wraps past midnight into the following day. The
camera records when ANY rule matches.

Fail-open: a malformed schedule records 24/7 (with a warning) — for an NVR,
silently losing footage is the worse failure mode. The API validator prevents
malformed schedules from being stored in the first place.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

import structlog

log = structlog.get_logger(__name__)


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _day_key(dt: datetime, mode: str) -> int:
    return dt.weekday() if mode == "weekly" else dt.day


def is_active(schedule: Optional[dict[str, Any]], now: Optional[datetime] = None) -> bool:
    """True when the schedule says the camera should be recording right now."""
    if not schedule:
        return True
    try:
        mode = schedule["mode"]
        rules = schedule["rules"]
        if mode not in ("weekly", "monthly") or not rules:
            raise ValueError(f"bad mode/rules: {mode!r}")

        now = now or datetime.now().astimezone()
        nmin = now.hour * 60 + now.minute
        prev_day = now - timedelta(days=1)

        for rule in rules:
            days = rule["days"]
            start = _minutes(rule.get("start") or "00:00")
            end = _minutes(rule.get("end") or "24:00")
            if end > start:
                if _day_key(now, mode) in days and start <= nmin < end:
                    return True
            else:
                # Wraps midnight: [start → 24:00] on the rule's day, plus
                # [00:00 → end] on the following day.
                if _day_key(now, mode) in days and nmin >= start:
                    return True
                if _day_key(prev_day, mode) in days and nmin < end:
                    return True
        return False
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        log.warning("schedule.malformed_failing_open", schedule=schedule, error=str(exc))
        return True
