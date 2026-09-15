"""scan_manager.py — one scan at a time, across workers, without touching
cameras that are already registered.

WHY IT IS RISKY. This API runs multiple uvicorn workers, so the worker that
receives POST /scan is rarely the one that answers the next GET /scan poll. The
job therefore lives in Valkey rather than in a module global, and the mutual
exclusion is a SET-NX lock. Two things follow, and both are the kind of defect
that only appears under load or on a second appliance:

  * a lock that is not actually exclusive lets two scans sweep the same LAN at
    once, doubling the probe traffic a camera sees and racing each other's
    progress counters;
  * the dedupe that protects REGISTERED rows is what stops a rescan from
    rewriting a working camera's lifecycle stage. `probe_device` says so in its
    own docstring — "never call this on a registered row, it rewrites the
    lifecycle stage" — and `_get_or_create_staging` is the guard that makes
    sure it is not.

The subnet memory is the third piece: once any camera has ever been found,
future zero-config scans sweep its /24. It must never widen to a public range.

Hermetic: Valkey is an in-memory stand-in and the database is a stub session.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.models import CameraStage  # noqa: E402
from backend.services import scan_manager as sm  # noqa: E402


class FakeRedis:
    """Only the commands scan_manager issues, with SET-NX that really is NX."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)
            self.hashes.pop(k, None)
        return 1

    async def hset(self, key, field=None, value=None, mapping=None):
        bucket = self.hashes.setdefault(key, {})
        if mapping:
            bucket.update({str(k): str(v) for k, v in mapping.items()})
        elif field is not None:
            bucket[str(field)] = str(value)
        return 1

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hincrby(self, key, field, amount=1):
        b = self.hashes.setdefault(key, {})
        b[field] = str(int(b.get(field, 0)) + amount)
        return int(b[field])

    async def expire(self, key, seconds):
        return True


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()

    async def _get():
        return fake

    monkeypatch.setattr(sm.redis_client, "get_redis", _get)
    # start_scan launches the scan as a background task; these tests are about
    # the lock and the job record it writes, not about the sweep.
    monkeypatch.setattr(sm.asyncio, "create_task", lambda coro: coro.close())
    return fake


# ── One scan at a time, across workers ─────────────────────────────────────

class TestScanLock:
    @pytest.mark.asyncio
    async def test_a_scan_starts_and_records_its_job(self, redis):
        job = await sm.start_scan("10.0.0.0/24", None, None)
        assert job["status"] == "running"
        assert job["cidr"] == "10.0.0.0/24"
        assert job["auto"] is False

    @pytest.mark.asyncio
    async def test_a_second_scan_is_refused_while_one_is_running(self, redis):
        await sm.start_scan("10.0.0.0/24", None, None)
        with pytest.raises(sm.ScanAlreadyRunning):
            await sm.start_scan("10.0.1.0/24", None, None)

    @pytest.mark.asyncio
    async def test_the_refusal_does_not_disturb_the_running_job(self, redis):
        first = await sm.start_scan("10.0.0.0/24", None, None)
        with pytest.raises(sm.ScanAlreadyRunning):
            await sm.start_scan("10.0.1.0/24", None, None)
        assert (await sm.current_job())["cidr"] == first["cidr"], (
            "the rejected scan overwrote the running job's record"
        )

    @pytest.mark.asyncio
    async def test_the_lock_carries_an_expiry_so_a_crash_cannot_wedge_scanning(self, redis):
        # A worker that dies mid-scan leaves the key behind. Without a TTL,
        # discovery is dead until somebody clears Valkey by hand.
        await sm.start_scan("10.0.0.0/24", None, None)
        assert sm._LOCK_TTL_SEC > 0
        assert sm._LOCK_KEY in redis.store

    @pytest.mark.asyncio
    async def test_a_zero_config_scan_records_itself_as_automatic(self, redis):
        job = await sm.start_scan(None, None, None)
        assert job["auto"] is True
        assert "auto" in job["cidr"]

    @pytest.mark.asyncio
    async def test_credentials_are_recorded_as_a_flag_never_as_a_value(self, redis):
        # The job record is readable by any authenticated caller polling
        # GET /scan. A password in it would be a credential leak into the UI.
        job = await sm.start_scan("10.0.0.0/24", "admin", "hunter2")
        assert job["with_credentials"] is True
        assert "hunter2" not in str(job)
        assert "hunter2" not in str(redis.hashes)

    @pytest.mark.asyncio
    async def test_a_scan_without_credentials_says_so(self, redis):
        assert (await sm.start_scan("10.0.0.0/24", None, None))["with_credentials"] is False

    @pytest.mark.asyncio
    async def test_a_username_with_no_password_is_not_credentialed(self, redis):
        assert (await sm.start_scan("10.0.0.0/24", "admin", ""))["with_credentials"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["not-a-cidr", "10.0.0.0/33", "10.0.0.256/24",
                                     "10.0.0.0/24 ", "1.2.3"])
    async def test_a_malformed_range_is_refused_before_the_lock_is_taken(self, redis, bad):
        # Taking the lock first would leave discovery wedged for an hour
        # because somebody mistyped a range.
        with pytest.raises(ValueError):
            await sm.start_scan(bad, None, None)
        assert sm._LOCK_KEY not in redis.store

    @pytest.mark.asyncio
    async def test_an_empty_range_means_auto_not_malformed(self, redis):
        # The form submits "" for "scan automatically". It is an ABSENT range,
        # not a bad one — `if cidr:` is what makes the distinction, and
        # validating it would reject the default path.
        job = await sm.start_scan("", None, None)
        assert job["auto"] is True

    @pytest.mark.asyncio
    async def test_a_single_host_range_is_accepted(self, redis):
        # "Manual IP" add becomes a /32.
        assert (await sm.start_scan("10.0.0.5/32", None, None))["status"] == "running"

    @pytest.mark.asyncio
    async def test_no_job_reports_none_rather_than_an_empty_shell(self, redis):
        # The UI distinguishes "never scanned" from "scan finished with no
        # results"; an empty dict would render as the second.
        assert await sm.current_job() is None


class TestJobFieldTypes:
    """The job record round-trips through a Valkey hash, where everything is a
    string. The UI switches on these, so the types have to survive."""

    @pytest.mark.asyncio
    async def test_counters_come_back_as_integers(self, redis):
        await sm.start_scan("10.0.0.0/24", None, None)
        await sm._job_set(total=12, scanned=5)
        job = await sm.current_job()
        assert job["total"] == 12 and job["scanned"] == 5
        assert isinstance(job["total"], int)

    @pytest.mark.asyncio
    async def test_flags_come_back_as_booleans(self, redis):
        await sm.start_scan(None, None, None)
        job = await sm.current_job()
        assert job["auto"] is True
        assert isinstance(job["with_credentials"], bool)

    @pytest.mark.asyncio
    async def test_an_absent_value_is_none_not_the_string_none(self, redis):
        await sm.start_scan("10.0.0.0/24", None, None)
        assert (await sm.current_job())["error"] is None


# ── Which ONVIF ports a device gets probed on ──────────────────────────────

class TestOnvifPortSelection:
    def _cam(self, open_ports=None, onvif_port=None):
        return SimpleNamespace(open_ports=open_ports, onvif_port=onvif_port)

    @pytest.fixture(autouse=True)
    def ports(self, monkeypatch):
        monkeypatch.setattr(sm.settings, "discovery_onvif_ports", [80, 8000, 8080])

    def test_only_configured_onvif_ports_are_considered(self):
        assert sm._onvif_ports_for(self._cam([554, 8000, 9999])) == [8000]

    def test_a_known_onvif_port_is_tried_first(self):
        assert sm._onvif_ports_for(self._cam([80, 8000], onvif_port=8000))[0] == 8000

    def test_a_device_with_no_open_onvif_port_still_gets_probed(self):
        # VMS-27: the sweep can miss filtered ports, and Docker Desktop's NAT
        # swallows refusals. Without the fallback such a device is stuck at
        # no_onvif forever, no matter what credentials an operator supplies.
        assert sm._onvif_ports_for(self._cam([554])) == [80, 8000, 8080]

    def test_a_device_with_no_ports_at_all_gets_the_full_fallback(self):
        assert sm._onvif_ports_for(self._cam(None)) == [80, 8000, 8080]

    def test_a_stale_onvif_port_not_in_the_open_set_is_not_promoted(self):
        # It is only moved to the front if the sweep also found it open.
        assert sm._onvif_ports_for(self._cam([8080], onvif_port=8000)) == [8080]


# ── Rescan dedupe: a registered camera is never re-staged ──────────────────

class StubDB:
    def __init__(self, rows):
        self._rows = rows
        self.added: list = []

    async def execute(self, *a, **kw):
        rows = self._rows
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


class TestStagingDedupe:
    @pytest.mark.asyncio
    async def test_an_ip_owned_by_a_registered_camera_is_left_alone(self):
        # THE GUARD. probe_device rewrites the lifecycle stage, so re-staging a
        # registered camera would take a working, recording camera back to
        # "discovered" on the next routine scan.
        registered = SimpleNamespace(ip="10.0.0.5",
                                     stage=CameraStage.REGISTERED.value)
        db = StubDB([registered])
        assert await sm._get_or_create_staging(db, "10.0.0.5", [554]) is None
        assert db.added == []

    @pytest.mark.asyncio
    async def test_an_unknown_ip_gets_a_new_staging_row(self):
        db = StubDB([])
        row = await sm._get_or_create_staging(db, "10.0.0.9", [554, 8000])
        assert row is not None
        assert db.added == [row]
        assert row.stage == CameraStage.DISCOVERED.value

    @pytest.mark.asyncio
    async def test_a_new_staging_row_is_never_live(self):
        # Staging rows must not be relayed, recorded or health-checked; a
        # staging row created enabled would be picked up by the health monitor
        # and reported as a camera that is down.
        db = StubDB([])
        row = await sm._get_or_create_staging(db, "10.0.0.9", [554])
        assert row.enabled is False

    @pytest.mark.asyncio
    async def test_an_existing_staging_row_is_updated_not_duplicated(self):
        existing = SimpleNamespace(ip="10.0.0.9", open_ports=[554],
                                   stage=CameraStage.DISCOVERED.value, enabled=False)
        db = StubDB([existing])
        row = await sm._get_or_create_staging(db, "10.0.0.9", [554, 8000])
        assert row is existing
        assert row.open_ports == [554, 8000]
        assert db.added == []

    @pytest.mark.asyncio
    async def test_a_registered_row_wins_over_a_staging_row_on_the_same_ip(self):
        # Both can exist if a camera was registered after being staged.
        db = StubDB([
            SimpleNamespace(ip="10.0.0.5", stage=CameraStage.DISCOVERED.value),
            SimpleNamespace(ip="10.0.0.5", stage=CameraStage.REGISTERED.value),
        ])
        assert await sm._get_or_create_staging(db, "10.0.0.5", [554]) is None


# ── The subnet memory that makes zero-config work ──────────────────────────

class TestKnownCameraSubnets:
    def _with_ips(self, monkeypatch, ips):
        class _DB:
            async def execute(self, *a, **kw):
                return SimpleNamespace(
                    scalars=lambda: SimpleNamespace(all=lambda: ips))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(sm, "AsyncSessionLocal", lambda: _DB())

    @pytest.mark.asyncio
    async def test_each_camera_contributes_its_slash_24(self, monkeypatch):
        self._with_ips(monkeypatch, ["192.168.1.50", "10.0.0.7"])
        assert await sm._known_camera_subnets() == ["10.0.0.0/24", "192.168.1.0/24"]

    @pytest.mark.asyncio
    async def test_cameras_on_one_subnet_yield_one_entry(self, monkeypatch):
        self._with_ips(monkeypatch, ["192.168.1.50", "192.168.1.51", "192.168.1.99"])
        assert await sm._known_camera_subnets() == ["192.168.1.0/24"]

    @pytest.mark.asyncio
    async def test_a_public_address_never_becomes_a_scan_range(self, monkeypatch):
        # Sweeping a public /24 is scanning somebody else's network. The
        # private check is the only thing preventing it, and a camera reachable
        # over a public address is exactly the case that would trigger it.
        self._with_ips(monkeypatch, ["8.8.8.8", "192.168.1.50"])
        assert await sm._known_camera_subnets() == ["192.168.1.0/24"]

    @pytest.mark.asyncio
    async def test_a_malformed_address_is_skipped_not_fatal(self, monkeypatch):
        self._with_ips(monkeypatch, ["not-an-ip", "192.168.1.50"])
        assert await sm._known_camera_subnets() == ["192.168.1.0/24"]

    @pytest.mark.asyncio
    async def test_an_empty_registry_yields_no_subnets(self, monkeypatch):
        self._with_ips(monkeypatch, [])
        assert await sm._known_camera_subnets() == []
