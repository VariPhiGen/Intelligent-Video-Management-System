"""Retention is minute-granular, and the sweep keeps up with it.

Two halves, and the second is the one that makes the first mean anything.

THE UNIT. `retention_minutes` is the canonical spelling everywhere — the
column, this service's API, the Smart Search deadline stamp, the compliance
report. Days were never a product decision, just the unit the first
implementation picked, and they made a whole class of deployment inexpressible:
a lobby camera kept for twenty minutes, a rig kept for an hour, a DPDP
data-minimisation posture that says "as long as the shift takes and no longer".
The floor is physical rather than arbitrary — segments are 60 s and retention
deletes whole ones, so a minute is the finest period this recorder can honour.

THE CADENCE. The pass ran hourly, which was fine while a day was the finest
period expressible. It is not fine for a ten-minute camera: footage would be
held for up to seventy minutes, the policy would be wrong by a factor of seven,
and an operator would have no way to tell whether the setting had taken effect
at all. So the loop re-paces itself from the SHORTEST retention on the box,
after every pass rather than once at startup, because retention is edited at
runtime. The directory walks stay hourly regardless — a short retention asks
for segments to be deleted on time, not for the HLS cache to be swept forty
times an hour, and without that split shortening ONE camera would multiply the
I/O cost of every camera on the appliance.

Run: python -m pytest services/nvr/tests/test_retention_minutes.py -q
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as nvr_main  # noqa: E402
from storage.retention import RetentionManager  # noqa: E402

DAY = 1440  # minutes


class FakeIndex:
    """Enough SegmentIndex for a retention pass. Records the cutoffs it was
    asked to delete before, which is the whole arithmetic under test."""

    def __init__(self, cameras=("gate",)):
        self._cameras = list(cameras)
        self.cutoffs: list[tuple[str, float]] = []

    def get_cameras(self):
        return list(self._cameras)

    def delete_before(self, camera, before_epoch):
        self.cutoffs.append((camera, before_epoch))
        return []

    def get_indexed_filepaths_since(self, camera, since):
        return set()

    def total_size(self, camera=None):
        return 0

    def select_oldest_until_under(self, limit):
        return []

    def delete_by_ids(self, ids):
        return None


def manager(tmp_path, cameras_config=None, default_minutes=30 * DAY,
            provider=None, **kw):
    return RetentionManager(
        FakeIndex(), cameras_config or [], str(tmp_path / "clips"),
        storage_path=str(tmp_path / "nvr"),
        default_retention_minutes=default_minutes,
        retention_map_provider=provider,
        **kw,
    )


def run_twice(rm):
    """Two passes: the first only establishes the clock baseline (age-based
    deletion is deferred until wall and monotonic clocks can be compared), so
    the cutoffs under test come from the second."""
    rm.run_once()
    rm._index.cutoffs.clear()  # noqa: SLF001 — the recorded arithmetic
    rm.run_once()


# ── the unit ─────────────────────────────────────────────────────────────────

def test_the_cutoff_is_computed_in_minutes(tmp_path):
    """A twenty-minute camera deletes twenty minutes back, not twenty days."""
    rm = manager(tmp_path, provider=lambda: {"gate": 20})
    before = time.time()
    run_twice(rm)
    [(_cam, cutoff)] = rm._index.cutoffs  # noqa: SLF001
    assert before - 20 * 60 - 5 <= cutoff <= time.time() - 20 * 60 + 5


def test_a_day_scale_policy_is_unchanged_by_the_unit_move(tmp_path):
    """The upgrade must not quietly re-time an existing 30-day fleet."""
    rm = manager(tmp_path, default_minutes=30 * DAY)
    before = time.time()
    run_twice(rm)
    [(_cam, cutoff)] = rm._index.cutoffs  # noqa: SLF001
    assert before - 30 * 86400 - 5 <= cutoff <= time.time() - 30 * 86400 + 5


def test_cameras_yaml_may_still_spell_retention_in_days(tmp_path):
    """An untouched cameras.yaml keeps its policy across the upgrade."""
    rm = manager(tmp_path, cameras_config=[{"name": "gate", "retention_days": 2}])
    run_twice(rm)
    [(_cam, cutoff)] = rm._index.cutoffs  # noqa: SLF001
    assert abs((time.time() - cutoff) - 2 * 86400) < 5


def test_minutes_win_over_days_when_a_config_carries_both(tmp_path):
    """Minutes is canonical; the day key is a legacy spelling, never an
    override of the explicit one."""
    rm = manager(tmp_path, cameras_config=[
        {"name": "gate", "retention_days": 2, "retention_minutes": 20}])
    run_twice(rm)
    [(_cam, cutoff)] = rm._index.cutoffs  # noqa: SLF001
    assert abs((time.time() - cutoff) - 20 * 60) < 5


def test_the_runtime_provider_still_wins_over_the_file(tmp_path):
    """Registry-synced cameras carry their own retention and are absent from
    cameras.yaml; the provider is how they are aged out at all."""
    rm = manager(tmp_path,
                 cameras_config=[{"name": "gate", "retention_minutes": 9999}],
                 provider=lambda: {"gate": 15})
    run_twice(rm)
    [(_cam, cutoff)] = rm._index.cutoffs  # noqa: SLF001
    assert abs((time.time() - cutoff) - 15 * 60) < 5


# ── the cadence ──────────────────────────────────────────────────────────────

def test_a_day_scale_fleet_is_still_swept_hourly(tmp_path):
    """No new I/O on the deployment that exists today."""
    rm = manager(tmp_path, default_minutes=30 * DAY)
    assert rm.sweep_interval() == RetentionManager.MAX_SWEEP_INTERVAL_S


def test_a_minute_scale_camera_pulls_the_sweep_in(tmp_path):
    """THE BUG THIS PREVENTS. On the old fixed hour, a camera set to ten
    minutes kept footage for up to seventy — the setting would have looked
    broken, and the appliance would have been holding footage it reported as
    deleted."""
    rm = manager(tmp_path, provider=lambda: {"gate": 10})
    assert rm.sweep_interval() == 10 * 60 / RetentionManager.SWEEP_DIVISOR


def test_the_shortest_camera_paces_the_whole_pass(tmp_path):
    """One camera's policy sets the cadence, because the pass is fleet-wide.
    Taking the longest, or an average, would leave that one camera unswept."""
    rm = manager(tmp_path, provider=lambda: {"gate": 10, "yard": 30 * DAY})
    assert rm.sweep_interval() == 10 * 60 / RetentionManager.SWEEP_DIVISOR


def test_the_cadence_never_goes_below_the_floor(tmp_path):
    """A sweep is real I/O, and nothing below a minute can be honoured anyway:
    segments are 60 s and whole ones are deleted."""
    rm = manager(tmp_path, provider=lambda: {"gate": 1})
    assert rm.sweep_interval() == RetentionManager.MIN_SWEEP_INTERVAL_S


def test_the_cadence_is_re_read_after_every_pass_not_fixed_at_startup(tmp_path):
    """Retention is edited from the UI at runtime. A cadence computed once at
    boot would leave a camera just set to ten minutes being swept on whatever
    the fleet's policy was an hour ago."""
    live = {"gate": 30 * DAY}
    rm = manager(tmp_path, provider=lambda: dict(live))
    assert rm.sweep_interval() == RetentionManager.MAX_SWEEP_INTERVAL_S
    live["gate"] = 10
    assert rm.sweep_interval() == 10 * 60 / RetentionManager.SWEEP_DIVISOR


def test_an_unreadable_camera_map_falls_back_to_hourly(tmp_path):
    """Pacing must never be the thing that stops retention running."""
    def boom():
        raise RuntimeError("provider down")
    rm = manager(tmp_path, provider=boom)
    assert rm.sweep_interval() == RetentionManager.MAX_SWEEP_INTERVAL_S


def test_the_loop_re_paces_itself_between_passes(tmp_path):
    """Wiring, not arithmetic: the numbers above are worthless if the loop
    still waits a fixed hour. Asserts the loop ASKS, and waits for what it is
    told."""
    rm = manager(tmp_path, provider=lambda: {"gate": 10})
    waits: list[float] = []

    class OneShot:
        """A stop event that lets exactly one pass through."""
        def is_set(self): return len(waits) >= 1
        def wait(self, seconds): waits.append(seconds); return True

    nvr_main.run_retention_loop(rm, OneShot())
    assert waits == [10 * 60 / RetentionManager.SWEEP_DIVISOR]


def test_an_explicit_interval_still_pins_the_loop(tmp_path):
    """Kept as an escape hatch for tests and for an operator who wants the old
    fixed behaviour."""
    rm = manager(tmp_path, provider=lambda: {"gate": 10})
    waits: list[float] = []

    class OneShot:
        def is_set(self): return len(waits) >= 1
        def wait(self, seconds): waits.append(seconds); return True

    nvr_main.run_retention_loop(rm, OneShot(), interval=3600)
    assert waits == [3600.0]


# ── the fast/slow split ──────────────────────────────────────────────────────

def test_the_directory_walks_stay_hourly_however_fast_the_sweep_runs(tmp_path):
    """Without this, shortening ONE camera to ten minutes would make every
    camera on the box re-glob its segment directory and re-read its whole
    index six times an hour. Age-based deletion is what the short retention
    asked for; the housekeeping is not."""
    rm = manager(tmp_path, provider=lambda: {"gate": 10})
    orphan_passes = []
    rm._clean_orphans = lambda cam, cutoff: orphan_passes.append(cam) or 0  # noqa: SLF001

    rm.run_once()          # baseline pass: does the housekeeping, defers ageing
    rm.run_once()          # ages out, and is NOT due for housekeeping again
    rm.run_once()
    assert len(rm._index.cutoffs) == 2, "age-based deletion must run every pass"  # noqa: SLF001
    assert orphan_passes == [], "orphan globbing is not due again within the hour"


def test_housekeeping_runs_again_once_its_own_interval_has_passed(tmp_path):
    """Throttled, not disabled. Orphaned .ts files are invisible to the indexed
    sweep, so something still has to collect them."""
    rm = manager(tmp_path, provider=lambda: {"gate": 10})
    orphan_passes = []
    rm._clean_orphans = lambda cam, cutoff: orphan_passes.append(cam) or 0  # noqa: SLF001

    rm.run_once()   # baseline
    rm.run_once()   # ages out; housekeeping not due
    assert orphan_passes == []
    # Pretend an hour of monotonic time went by since the last housekeeping.
    rm._last_housekeeping_mono -= RetentionManager.HOUSEKEEPING_INTERVAL_S + 1  # noqa: SLF001
    rm.run_once()
    assert orphan_passes == ["gate"]


def test_the_disk_floor_is_not_throttled_with_the_housekeeping(tmp_path):
    """It is one statvfs when there is no pressure, and it is the last thing
    between a full disk and recording stopping altogether."""
    rm = manager(tmp_path, provider=lambda: {"gate": 10})
    calls = []
    rm.enforce_disk_floor = lambda: calls.append(1) or 0
    rm.run_once()
    rm.run_once()
    rm.run_once()
    assert len(calls) == 3


# ── what the alert surfaces report ───────────────────────────────────────────

def test_the_longest_and_shortest_retention_are_both_reportable(tmp_path):
    rm = manager(tmp_path, provider=lambda: {"gate": 10, "yard": 30 * DAY})
    assert rm.configured_retention_minutes() == 30 * DAY
    assert rm.shortest_retention_minutes() == 10
