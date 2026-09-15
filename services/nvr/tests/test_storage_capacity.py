"""Unit tests for storage/capacity.py — the disk-derived storage-cap ceiling.

The point of the module is that a cap larger than the volume is not a bigger
limit: RetentionManager's disk-floor evictor reclaims space before such a cap is
ever reached, so the number is saved, displayed, and never enforced. These tests
pin the arithmetic that decides where that line falls, plus the two behaviours
that are easy to regress — counting already-recorded footage (without it, a
capped-and-full volume reports a maximum near zero and the operator cannot even
re-save the cap they already have) and failing open when the disk cannot be
measured.

shutil.disk_usage is monkeypatched throughout: the numbers have to be exact and
the same on Linux, macOS and Windows CI, which a real filesystem cannot give.

Run: python -m pytest services/nvr/tests -q
"""
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage import capacity  # noqa: E402

GB = 1024 ** 3


def fake_usage(monkeypatch, total_gb, free_gb):
    """Pin shutil.disk_usage to an exact total/free, in GB."""
    total, free = int(total_gb * GB), int(free_gb * GB)
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda _p: shutil._ntuple_diskusage(total, total - free, free),
    )


def test_max_cap_is_free_plus_footage_less_reserve(tmp_path, monkeypatch):
    fake_usage(monkeypatch, total_gb=1000, free_gb=400)
    head = capacity.probe(tmp_path, footage_bytes=500 * GB)
    # 400 free + 500 recorded - 8% of 1000 = 820 GB.
    assert head is not None
    assert head.max_cap_gb == 820
    assert head.max_cap_bytes == 500 * GB + 400 * GB - int(1000 * GB * 8.0 / 100)


def test_footage_counts_toward_the_ceiling(tmp_path, monkeypatch):
    """The regression that makes the feature unusable if it is ever dropped.

    A volume held near its cap has little free space left. If the ceiling counted
    only free space, the maximum offered to an operator with a 900 GB cap would
    be ~20 GB — they could not keep, let alone raise, the cap they already run.
    Counting footage makes the ceiling a statement about the volume rather than
    about this instant's leftovers.
    """
    fake_usage(monkeypatch, total_gb=1000, free_gb=20)
    head = capacity.probe(tmp_path, footage_bytes=900 * GB)
    assert head.max_cap_gb == 840  # 900 recorded + 20 free - 80 reserve
    # Far above the 20 GB of free space, which is all a free-space-only ceiling
    # could have offered (it would in fact clamp to 0 here, the reserve being
    # larger than what is free).
    assert head.max_cap_bytes > head.free_bytes * 40


def test_ceiling_can_sit_below_a_cap_that_is_already_set(tmp_path, monkeypatch):
    """Over-committed volumes are told to come down, and that is correct.

    900 GB of footage on a 1 TB disk with only 20 GB free means 80 GB of
    non-footage data is already inside the reserve. The 900 GB cap cannot be
    honoured — the disk-floor evictor is about to fire regardless of it — so the
    ceiling lands below the current cap and the operator must lower it. Pinned
    because it is the one case where the API refuses a value it previously
    accepted, and that has to be deliberate rather than a surprise.
    """
    fake_usage(monkeypatch, total_gb=1000, free_gb=20)
    head = capacity.probe(tmp_path, footage_bytes=900 * GB)
    assert head.max_cap_bytes < 900 * GB
    # Whereas the same volume with the reserve intact keeps the cap settable.
    fake_usage(monkeypatch, total_gb=1000, free_gb=100)
    ok = capacity.probe(tmp_path, footage_bytes=900 * GB)
    assert ok.max_cap_bytes >= 900 * GB


def test_ceiling_never_goes_negative(tmp_path, monkeypatch):
    """Inside the reserve there is nothing to grant — 0, not a negative number."""
    fake_usage(monkeypatch, total_gb=1000, free_gb=1)
    head = capacity.probe(tmp_path, footage_bytes=0)
    assert head.max_cap_bytes == 0
    assert head.max_cap_gb == 0


def test_max_cap_gb_floors_so_the_advertised_value_is_settable(tmp_path, monkeypatch):
    """The API takes GB and converts back to bytes.

    Rounding the advertised maximum up would produce a number the server's own
    validation then rejects — the UI's "Use maximum" button would fail.
    """
    fake_usage(monkeypatch, total_gb=100, free_gb=50.6)
    head = capacity.probe(tmp_path, footage_bytes=0)
    assert head.max_cap_gb * GB <= head.max_cap_bytes


def test_reserve_tracks_the_evictor_recovery_target():
    """The reserve exists because of the evictor, so it must not drift from it."""
    from storage.retention import RetentionManager
    assert capacity.RESERVE_FREE_PCT == RetentionManager.DISK_TARGET_FREE_PCT
    # Recovery target, not the trigger: a cap set to exactly the maximum should
    # land above the floor rather than oscillating on it every health cycle.
    assert capacity.RESERVE_FREE_PCT > RetentionManager.DISK_FLOOR_FREE_PCT


def test_probe_walks_up_to_an_existing_ancestor(tmp_path, monkeypatch):
    """The storage dir may not exist yet on a first boot — the volume still does."""
    fake_usage(monkeypatch, total_gb=100, free_gb=50)
    missing = tmp_path / "not" / "created" / "yet"
    head = capacity.probe(missing, footage_bytes=0)
    assert head is not None
    assert head.path == str(tmp_path)


def test_probe_returns_none_when_the_disk_cannot_be_measured(tmp_path, monkeypatch):
    """Callers fail open on None: a stat failure must not block setting a cap."""
    def boom(_p):
        raise OSError("device not ready")
    monkeypatch.setattr(shutil, "disk_usage", boom)
    assert capacity.probe(tmp_path, footage_bytes=0) is None


def test_probe_returns_none_for_an_unrooted_path(monkeypatch):
    """A missing Windows drive letter has no existing ancestor — and must not loop."""
    called = []
    monkeypatch.setattr(shutil, "disk_usage", lambda p: called.append(p))
    monkeypatch.setattr(Path, "exists", lambda self: False)
    assert capacity.probe("/nonexistent/volume/footage") is None
    assert not called


def test_probe_of_none_is_none():
    """create_app may have no storage path wired; that is not an error."""
    assert capacity.probe(None) is None


@pytest.mark.parametrize("size,expected", [
    (512 * GB, "512.00 GB"),
    (1024 ** 4, "1.00 TB"),
    (3 * 1024 ** 4 + 512 * GB, "3.50 TB"),
])
def test_humanize_switches_to_tb_at_a_terabyte(size, expected):
    assert capacity.humanize(size) == expected
