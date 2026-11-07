"""rules.py — what a rule matches. Pure functions, no I/O, no ambient clock.

Every decision a rule makes lives here so it can be tested without a database,
a broker or a running clock: `now` is always a parameter. The impure half —
caching, cooldown, writing the alert — is services/rules_engine.py.

The condition language is deliberately small. It is stored in a database row
and rendered in a dropdown UI, so it must be serialisable and inspectable, and
it must not be able to execute anything. There is no eval, no lambda and no
attribute traversal beyond one level into `attributes`.

Nothing in this module raises. A malformed rule is a rule that does not match;
an operator's typo must never 500 the ingest endpoint and cost the rest of the
batch its events.
"""
from __future__ import annotations

import re
from datetime import datetime, time
from typing import Any, Mapping, NamedTuple, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Leaf operators. Anything not in here is an unknown op and matches nothing.
_OPS = ("eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "matches", "exists")


def _resolve(field: Any, event: Mapping[str, Any]) -> "tuple[bool, Any]":
    """(present, value). Presence is separate from value so `exists` can tell a
    missing key from a stored False."""
    if not isinstance(field, str) or not field:
        return False, None
    if field.startswith("attributes."):
        attrs = event.get("attributes")
        if not isinstance(attrs, Mapping):
            return False, None
        key = field[len("attributes."):]
        return (key in attrs), attrs.get(key)
    return (field in event), event.get(field)


def _leaf(clause: Any, event: Mapping[str, Any]) -> bool:
    if not isinstance(clause, Mapping):
        return False
    op = clause.get("op")
    if op not in _OPS:
        return False
    present, actual = _resolve(clause.get("field"), event)
    expected = clause.get("value")

    if op == "exists":
        return present is bool(expected)
    if not present:
        return False

    try:
        if op == "eq":
            return actual == expected
        if op == "ne":
            return actual != expected
        if op == "in":
            return isinstance(expected, (list, tuple)) and actual in expected
        if op == "not_in":
            return isinstance(expected, (list, tuple)) and actual not in expected
        if op == "matches":
            # Only strings are matchable. A plate that arrived as a number is a
            # non-match, not a TypeError.
            if not isinstance(actual, str) or not isinstance(expected, str):
                return False
            return re.search(expected, actual) is not None
        if op == "gt":
            return actual > expected
        if op == "gte":
            return actual >= expected
        if op == "lt":
            return actual < expected
        if op == "lte":
            return actual <= expected
    except (TypeError, ValueError, re.error):
        # Incomparable types, or an operator-supplied regex that does not
        # compile. Both are "does not match", never a crash.
        return False
    return False


def matches_conditions(conditions: Any, event: Mapping[str, Any]) -> bool:
    """Evaluate a stored `conditions` document against one event.

    `{}` matches everything — a rule with no extra conditions is triggered by
    its activity and scope alone, which is the common case.
    """
    if not conditions:
        return True
    if not isinstance(conditions, Mapping):
        return False
    if "all" in conditions:
        clauses = conditions["all"]
        if not isinstance(clauses, list):
            return False
        return all(_leaf(c, event) for c in clauses)
    if "any" in conditions:
        clauses = conditions["any"]
        if not isinstance(clauses, list):
            return False
        return any(_leaf(c, event) for c in clauses)
    return False


# ── Active hours ──────────────────────────────────────────────────────────────


def _parse_hhmm(raw: Any) -> "Optional[time]":
    if not isinstance(raw, str):
        return None
    parts = raw.split(":")
    if len(parts) != 2:
        return None
    try:
        hh, mm = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return time(hh, mm)


def within_active_hours(active_hours: "Optional[Mapping[str, Any]]", now: datetime,
                        *, site_tz: str) -> bool:
    """Is `now` inside one of the rule's armed windows?

    NULL `active_hours` means always armed — the common case, and the only case
    that means "always". A window list that is present but empty or malformed
    means NOT armed: an operator who typed a bad time should get a rule that
    stays quiet, not one that fires around the clock.

    Windows may cross midnight. `["22:00","06:00"]` is 22:00 today through
    06:00 tomorrow, tested as `t >= start or t < end` rather than
    `start <= t < end` — the latter makes such a window match nothing, silently.

    `days` (0 = Monday) binds to the day the window OPENS, so a Friday-night
    window still covers 02:00 on Saturday. Binding it to the instant would stop
    the alarm halfway through the night it was armed for.
    """
    if active_hours is None:
        return True
    if not isinstance(active_hours, Mapping):
        return False

    tz_name = active_hours.get("tz") or site_tz
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return False

    local = now.astimezone(tz)
    windows = active_hours.get("windows")
    if not isinstance(windows, list) or not windows:
        return False
    days = active_hours.get("days")
    if days is not None and not isinstance(days, list):
        return False

    t = local.time()
    for w in windows:
        if not isinstance(w, (list, tuple)) or len(w) != 2:
            return False
        start, end = _parse_hhmm(w[0]), _parse_hhmm(w[1])
        if start is None or end is None:
            return False

        if start <= end:
            inside = start <= t < end
            opened_on = local.weekday()
        else:
            # Crosses midnight.
            inside = t >= start or t < end
            # Before `end` means we are in the window that opened YESTERDAY.
            opened_on = (local.weekday() - 1) % 7 if t < end else local.weekday()

        if inside and (days is None or opened_on in days):
            return True
    return False


# ── The gate chain ────────────────────────────────────────────────────────────


class Decision(NamedTuple):
    """Why a rule did or did not fire, and whether it may touch the world.

    `fires` and `actuate` are separate because they answer different questions.
    A stale event still produces evidence (it happened) but must not move a
    barrier. A log_only rule is the same shape deliberately.
    """
    fires: bool
    reason: str
    actuate: bool


def _no(why: str) -> Decision:
    return Decision(False, why, False)


def should_fire(rule: Mapping[str, Any], event: Mapping[str, Any], now: datetime,
                *, site_tz: str) -> Decision:
    """Run the gate chain in the order spec 6.2 fixes.

    Order is not arbitrary: the cheap discriminating gates come first so a busy
    camera's events are rejected on a string compare rather than a predicate
    walk or a timezone conversion.
    """
    if not rule.get("enabled"):
        return _no("disabled")

    if event.get("activity") != rule.get("activity"):
        return _no("activity")

    kind = rule.get("scope_kind")
    if kind == "camera":
        if event.get("camera_slug") != rule.get("scope_value"):
            return _no("scope")
    elif kind == "zone":
        # zone=None is a whole-frame detection — a real config, not "unset".
        # A zone-scoped rule must not adopt it.
        if event.get("zone") is None or event.get("zone") != rule.get("scope_value"):
            return _no("scope")
    elif kind != "any":
        return _no("scope")

    floor = rule.get("min_confidence")
    if floor is not None:
        conf = event.get("confidence")
        # NULL confidence means the detector reported none, which is not 0.0.
        # A rule with a threshold cannot be satisfied by "unknown".
        if conf is None or conf < floor:
            return _no("confidence")

    if not matches_conditions(rule.get("conditions"), event):
        return _no("conditions")

    if not within_active_hours(rule.get("active_hours"), now, site_tz=site_tz):
        return _no("schedule")

    # From here the rule fires. What remains is whether it may actuate.
    started = event.get("started_at")
    max_age = rule.get("max_event_age_s")
    if started is not None and max_age is not None:
        age = (now - started).total_seconds()
        # Only lateness disables actuation. A camera clock running slightly
        # ahead gives a negative age and is not staleness.
        if age > max_age:
            return Decision(True, "stale", False)

    if rule.get("mode") != "armed":
        return Decision(True, "log_only", False)

    return Decision(True, "fires", True)
