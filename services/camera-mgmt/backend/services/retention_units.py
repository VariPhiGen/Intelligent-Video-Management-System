"""retention_units.py — one definition of what a retention period IS.

Retention used to be a whole number of DAYS everywhere: the column, the NVR
API, the Smart Search deadline stamp, the compliance report. That floor was
never a product decision, it was just the unit the first implementation
picked, and it made a whole class of deployment impossible to express — a
lobby camera kept for twenty minutes, a test rig kept for an hour, a DPDP
data-minimisation posture that says "we hold this for as long as the shift
takes and no longer".

Minutes are the canonical unit now, and the floor is physical rather than
arbitrary: the recorder writes 60-second MPEG-TS segments and retention
deletes whole segments, so one minute is the finest period that can mean
anything, and deletion is accurate to within one segment either way.

Everything that derives a deletion deadline from a camera's retention — the
NVR's own sweep, the Smart Search index's ``expires_at``, AI event rows,
heatmap buckets, the sub track's clamp — reads MINUTES through here. That is
the point of the module: the compliance surfaces report the same number the
sweeps enforce, because there is only one number and one conversion.

Days have NOT gone away as an *input*; they are a spelling of minutes. The
API still accepts ``retention_days``, the .env still carries
NVR_DEFAULT_RETENTION_DAYS, and the UI still offers days as a unit — they
are all converted here, at the edge, exactly once.
"""

from __future__ import annotations

MINUTES_PER_HOUR = 60
MINUTES_PER_DAY = 1440

#: The finest period the recorder can honour. Segments are 60 s, retention
#: deletes whole segments, so anything below this is a promise the storage
#: layer cannot keep.
MIN_RETENTION_MINUTES = 1

#: 3650 days, the ceiling the day-based API has always enforced. Kept so the
#: unit change does not quietly widen what an operator can set.
MAX_RETENTION_MINUTES = 3650 * MINUTES_PER_DAY


def days_to_minutes(days: int | float | None) -> int | None:
    """Convert a day-spelled retention to minutes. None passes through."""
    if days is None:
        return None
    return int(round(float(days) * MINUTES_PER_DAY))


def minutes_to_days(minutes: int | None) -> float | None:
    """Convert minutes to (possibly fractional) days, for display only.

    Never use this to compute a deadline — rounding a 30-minute retention to
    "0 days" is how a sweep silently stops deleting, and rounding it up to
    "1 day" is how the appliance reports a promise it is not keeping.
    """
    if minutes is None:
        return None
    return minutes / MINUTES_PER_DAY


def format_retention(minutes: int | None) -> str:
    """A human spelling of a retention period: "45 minutes", "6 hours", "30 days".

    Used by the compliance report and the audit trail, which previously wrote a
    bare integer and a hard-coded "days". A report that says "0 days" about a
    camera kept for twenty minutes is worse than no report.
    """
    if minutes is None:
        return "appliance default"
    minutes = int(minutes)
    if minutes % MINUTES_PER_DAY == 0:
        n = minutes // MINUTES_PER_DAY
        return f"{n} day" if n == 1 else f"{n} days"
    if minutes % MINUTES_PER_HOUR == 0:
        n = minutes // MINUTES_PER_HOUR
        return f"{n} hour" if n == 1 else f"{n} hours"
    return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"


def default_retention_minutes() -> int:
    """The appliance default, in minutes.

    NVR_RETENTION_MINUTES wins when set; otherwise the long-standing
    NVR_DEFAULT_RETENTION_DAYS is read as a day-spelling of the same thing, so
    an appliance that was configured before minutes existed keeps its policy
    across the upgrade without anyone editing .env.
    """
    from ..config import settings  # noqa: PLC0415 — settings imports services

    if settings.nvr_retention_minutes:
        return int(settings.nvr_retention_minutes)
    return int(settings.nvr_default_retention_days) * MINUTES_PER_DAY


def default_sub_retention_minutes() -> int:
    """The appliance default for a sub track, in minutes. Same fallback rule."""
    from ..config import settings  # noqa: PLC0415 — see default_retention_minutes

    if settings.nvr_sub_retention_minutes:
        return int(settings.nvr_sub_retention_minutes)
    return int(settings.nvr_sub_retention_days) * MINUTES_PER_DAY


def effective_retention_minutes(camera_minutes: int | None) -> int:
    """A camera's retention as a CONCRETE number of minutes.

    NULL on the row means "the appliance default", which is a perfectly good
    answer for the recorder (it resolves the same default itself) but not for
    anything that has to STAMP a deadline on a row it is about to write. Those
    callers need the number, and they must get the same number the recorder
    will use.
    """
    return int(camera_minutes) if camera_minutes else default_retention_minutes()
