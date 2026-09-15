"""Unit tests for storage/alerts.py — the storage-pressure warning.

Three warning surfaces for the DISK already existed (header meter, log lines,
Storage tab) and all three need a human looking at something. The CAP had none
at all, and it is the failure that says nothing: footage is deleted exactly as
designed, nothing breaks, and a site configured for 30 days quietly keeps 11.

So what is pinned here is mostly the two ways a warning system fails in
practice — crying wolf, and going quiet:

  * A young install has little footage and a short on-disk span. Inferring
    "the cap is winning" from that span would warn on every new deployment
    until it filled up, and an operator who is warned wrongly once stops
    reading the warnings. Binding is therefore read from eviction EVENTS.
  * A standing critical must not re-notify every minute, and a restart must not
    replay it — or lose the all-clear, which is worse, because then the last
    thing anyone heard was CRITICAL.

evaluate() is pure, so every case below runs identically on Linux, macOS and
Windows with no filesystem and no clock.

Run: python -m pytest services/nvr/tests -q
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage import alerts  # noqa: E402
from storage.alerts import CRITICAL, OK, WARNING, StorageAlerter, evaluate  # noqa: E402

GB = 1024 ** 3
NOW = 1_700_000_000.0


def snap(**kw):
    """An evaluate() call with everything healthy unless overridden."""
    base = dict(
        disk_state={"used_pct": 40.0},
        footage_bytes=100 * GB,
        limit_bytes=None,
        pressure={},
        oldest_epoch=NOW - 30 * 86400,
        configured_days=30,
        now=NOW,
    )
    base.update(kw)
    return evaluate(**base)


# ── the quiet case ───────────────────────────────────────────────────────────

def test_healthy_is_silent():
    r = snap()
    assert r["state"] == OK
    assert r["reasons"] == []


def test_a_young_install_does_not_warn():
    """The false positive that would discredit the whole feature.

    Two days of footage against a 30-day policy looks exactly like a cap that
    has been evicting for months — unless you ask whether anything was actually
    evicted. Nothing has been, so this must be silent.
    """
    r = snap(oldest_epoch=NOW - 2 * 86400, configured_days=30, pressure={})
    assert r["state"] == OK
    assert r["effective_retention_days"] == 2.0   # reported, but not a warning


# ── the disk ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("pct,expected", [
    (89.9, OK), (90.0, WARNING), (94.9, WARNING), (95.0, CRITICAL), (99.0, CRITICAL),
])
def test_disk_thresholds(pct, expected):
    assert snap(disk_state={"used_pct": pct})["state"] == expected


def test_disk_thresholds_match_the_engine():
    """The meter, the log line and the notification must agree about 'full'."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from recorder.engine import RecordingEngine
    assert alerts.DISK_WARN_PCT == RecordingEngine.DISK_WARN_PCT
    assert alerts.DISK_CRIT_PCT == RecordingEngine.DISK_CRIT_PCT


def test_unmeasurable_disk_is_not_an_alert():
    """A missing probe is not evidence of a full disk."""
    assert snap(disk_state=None)["state"] == OK


# ── the cap ──────────────────────────────────────────────────────────────────

def test_cap_approaching_warns_before_anything_is_deleted():
    r = snap(footage_bytes=91 * GB, limit_bytes=100 * GB)
    assert r["state"] == WARNING
    assert r["cap_usage_pct"] == 91.0
    assert "starts being deleted" in r["reasons"][0]


def test_cap_below_threshold_is_silent():
    assert snap(footage_bytes=50 * GB, limit_bytes=100 * GB)["state"] == OK


def test_binding_cap_names_the_shortfall_not_the_mechanism():
    """An operator needs 'you are keeping 11 days, not 30' — not 'eviction ran'."""
    r = snap(pressure={"size_cap_last_at": NOW - 60},
             oldest_epoch=NOW - 11 * 86400, configured_days=30)
    assert r["state"] == WARNING
    assert r["cap_binding"] is True
    assert "configured for 30 days" in r["reasons"][0]
    assert "11.0 days old" in r["reasons"][0]


def test_binding_is_read_from_events_not_from_the_span():
    """The same short span, with and without an eviction behind it."""
    short = dict(oldest_epoch=NOW - 5 * 86400, configured_days=30)
    assert snap(pressure={}, **short)["state"] == OK
    assert snap(pressure={"size_cap_last_at": NOW - 60}, **short)["state"] == WARNING


def test_binding_expires_so_a_one_off_eviction_does_not_alert_forever():
    old = NOW - alerts.CAP_BINDING_WINDOW_S - 1
    assert snap(pressure={"size_cap_last_at": old})["cap_binding"] is False


def test_binding_window_spans_several_retention_passes():
    """Retention runs hourly; a window under ~2 passes would flap."""
    assert alerts.CAP_BINDING_WINDOW_S >= 2 * 3600


def test_binding_still_warns_without_a_configured_retention():
    """No camera map yet — still say the cap is evicting, just less precisely."""
    r = snap(pressure={"size_cap_last_at": NOW - 60}, configured_days=None)
    assert r["state"] == WARNING
    assert "evicting footage" in r["reasons"][0]


def test_disk_floor_eviction_is_critical_not_a_warning():
    """The volume ran out of space and retention was ignored outright."""
    r = snap(pressure={"disk_floor_last_at": NOW - 60})
    assert r["state"] == CRITICAL


def test_worst_condition_wins_and_every_reason_is_kept():
    r = snap(disk_state={"used_pct": 96.0},
             pressure={"size_cap_last_at": NOW - 60},
             oldest_epoch=NOW - 11 * 86400)
    assert r["state"] == CRITICAL          # disk beats the cap warning
    assert len(r["reasons"]) == 2          # but the cap reason is not discarded


# ── notification behaviour ───────────────────────────────────────────────────

def make_alerter(tmp_path, sent):
    a = StorageAlerter(state_path=tmp_path / "alert-state.json",
                       webhook_url="http://example.invalid/hook", interval_seconds=0)
    a._notify = lambda result, previous: sent.append((previous, result["state"]))  # noqa: SLF001
    return a


def test_notifies_only_on_transitions(tmp_path):
    """A standing critical at 60s would otherwise be 1,440 alerts a day, and an
    operator who filters those has no alerting at all."""
    sent = []
    a = make_alerter(tmp_path, sent)
    for _ in range(5):
        a.check(dict(disk_state={"used_pct": 96.0}, footage_bytes=0, limit_bytes=None,
                     pressure={}, oldest_epoch=None, configured_days=None, now=NOW))
    assert sent == [(None, CRITICAL)]


def test_the_all_clear_is_sent(tmp_path):
    sent = []
    a = make_alerter(tmp_path, sent)
    bad = dict(disk_state={"used_pct": 96.0}, footage_bytes=0, limit_bytes=None,
               pressure={}, oldest_epoch=None, configured_days=None, now=NOW)
    a.check(bad)
    a.check({**bad, "disk_state": {"used_pct": 40.0}})
    assert sent == [(None, CRITICAL), (CRITICAL, OK)]


def test_a_restart_does_not_replay_a_standing_alert(tmp_path):
    """...and, more importantly, does not swallow the all-clear afterwards."""
    sent1, sent2 = [], []
    bad = dict(disk_state={"used_pct": 96.0}, footage_bytes=0, limit_bytes=None,
               pressure={}, oldest_epoch=None, configured_days=None, now=NOW)
    a = make_alerter(tmp_path, sent1)
    a.check(bad)
    assert sent1 == [(None, CRITICAL)]

    b = make_alerter(tmp_path, sent2)        # "restart" — same state file
    b.check(bad)
    assert sent2 == []                       # not replayed
    b.check({**bad, "disk_state": {"used_pct": 40.0}})
    assert sent2 == [(CRITICAL, OK)]         # recovery still reaches the operator


def test_state_file_is_json_and_readable(tmp_path):
    a = make_alerter(tmp_path, [])
    a.check(dict(disk_state={"used_pct": 96.0}, footage_bytes=0, limit_bytes=None,
                 pressure={}, oldest_epoch=None, configured_days=None, now=NOW))
    written = json.loads((tmp_path / "alert-state.json").read_text())
    assert written["last_notified_state"] == CRITICAL


def test_a_corrupt_state_file_does_not_stop_alerting(tmp_path):
    (tmp_path / "alert-state.json").write_text("{ not json")
    sent = []
    a = make_alerter(tmp_path, sent)
    a.check(dict(disk_state={"used_pct": 96.0}, footage_bytes=0, limit_bytes=None,
                 pressure={}, oldest_epoch=None, configured_days=None, now=NOW))
    assert sent == [(None, CRITICAL)]


def test_a_failing_webhook_never_raises_into_the_nvr(tmp_path):
    """Alerting must not be able to take down recording. The URL is
    unresolvable, so this exercises the real urllib path."""
    a = StorageAlerter(state_path=tmp_path / "s.json",
                       webhook_url="http://127.0.0.1:1/hook",
                       interval_seconds=0, timeout_seconds=0.2)
    r = a.check(dict(disk_state={"used_pct": 96.0}, footage_bytes=0, limit_bytes=None,
                     pressure={}, oldest_epoch=None, configured_days=None, now=NOW))
    assert r["state"] == CRITICAL
    assert a._notify_failures == 1  # noqa: SLF001


def test_no_webhook_configured_still_evaluates_and_logs(tmp_path):
    """Log-only is a supported deployment, not a broken one."""
    a = StorageAlerter(state_path=tmp_path / "s.json", webhook_url="", interval_seconds=0)
    assert a.check(dict(disk_state={"used_pct": 96.0}, footage_bytes=0, limit_bytes=None,
                        pressure={}, oldest_epoch=None, configured_days=None,
                        now=NOW))["state"] == CRITICAL


def test_stop_is_safe_when_never_started(tmp_path):
    StorageAlerter(state_path=tmp_path / "s.json").stop()


def test_zero_interval_disables_the_thread(tmp_path):
    a = StorageAlerter(state_path=tmp_path / "s.json", interval_seconds=0)
    a.start(lambda: {})
    assert a._thread is None  # noqa: SLF001
