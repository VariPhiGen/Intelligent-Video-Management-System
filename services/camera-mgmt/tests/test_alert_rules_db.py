"""What 039 does to a real database — the half a stub cannot reach.

SKIPPED UNLESS YOU GIVE IT A SCRATCH DATABASE, with the same two rails as
test_activity_catalog_migration_db.py: DATABASE_URL is overridden for alembic
(inside the api container it names the LIVE database), and the scratch database
must start empty.

    RULES_TEST_DB=postgresql+asyncpg://user:pw@host:5432/scratch \
        python -m pytest services/camera-mgmt/tests/test_alert_rules_db.py

WHAT IT PROVES, none of which a unit test can:
  * a rule inserted with no mode is log_only — the default that stops an
    unreviewed rule from moving a barrier
  * the name index is case-insensitive
  * deleting a rule NULLs alerts.rule_id and keeps the alert, with its
    denormalised rule_name intact
  * mode, scope_kind and the scope_value pairing are enforced by the database,
    not only by pydantic
  * downgrade drops the table and the FK cleanly, so 039 is reversible
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest

DB_URL = os.environ.get("RULES_TEST_DB")

pytestmark = pytest.mark.skipif(
    not DB_URL, reason="set RULES_TEST_DB to a scratch database URL")

if DB_URL:                                  # keep collection cheap when skipping
    pytest.importorskip(
        "alembic", reason="alembic is not installed here (it lives in the api image)")
    asyncpg = pytest.importorskip("asyncpg")
    from alembic import command                               # noqa: E402
    from alembic.config import Config                         # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _alembic_url() -> str:
    return DB_URL if "+asyncpg" in DB_URL else DB_URL.replace("postgresql://",
                                                              "postgresql+asyncpg://")


def _asyncpg_dsn() -> str:
    return _alembic_url().replace("+asyncpg", "")


def _cfg() -> "Config":
    # RAIL 1. env.py prefers DATABASE_URL over anything passed here, and in the
    # api container that variable is the live database.
    os.environ["DATABASE_URL"] = _alembic_url()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", _alembic_url())
    return cfg


def _run(coro):
    return asyncio.run(coro)


async def _query(sql: str, *args):
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def _execute(sql: str, *args):
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        return await conn.execute(sql, *args)
    finally:
        await conn.close()


def _insert_rule(name="gate on intrusion", **cols):
    """Insert through raw SQL so the DATABASE's defaults and constraints are
    what answer, not SQLAlchemy's client-side ones."""
    rid = uuid.uuid4()
    base = {"activity": "unauthorised_access", "scope_kind": "any"}
    base.update(cols)
    keys = ", ".join(base)
    holes = ", ".join(f"${i + 3}" for i in range(len(base)))
    _run(_execute(
        f"INSERT INTO alert_rules (id, name, created_at, updated_at, {keys}) "
        f"VALUES ($1, $2, now(), now(), {holes})",
        rid, name, *base.values()))
    return rid


@pytest.fixture(scope="module")
def migrated():
    # RAIL 2: the scratch database must be empty.
    tables = _run(_query(
        "SELECT count(*) AS n FROM information_schema.tables "
        "WHERE table_schema = 'public'"))
    if tables[0]["n"]:
        pytest.skip("RULES_TEST_DB is not empty; refusing to migrate it")
    command.upgrade(_cfg(), "039")
    yield


@pytest.fixture(autouse=True)
def clean(migrated, monkeypatch):
    # This suite deliberately tests migration 039, before integration tables.
    # Integration-enabled rule firing is covered against head in test_event_delivery_db.
    from backend.config import settings
    monkeypatch.setattr(settings, "events_enabled", False)
    yield
    _run(_execute("DELETE FROM alerts"))
    _run(_execute("DELETE FROM alert_rules"))
    _run(_execute("DELETE FROM cameras"))


def _a_camera() -> uuid.UUID:
    """Alerts FK to a camera, so one has to exist.

    A registered camera must carry name, slug and rtsp_url —
    ck_cameras_registered_complete says so — and every other column has a
    server default."""
    cid = uuid.uuid4()
    _run(_execute(
        "INSERT INTO cameras (id, name, slug, rtsp_url) VALUES ($1, $2, $3, $4)",
        cid, "Test camera", f"cam-{cid.hex[:6]}", "rtsp://example.invalid/stream"))
    return cid


def test_a_rule_defaults_to_log_only(migrated):
    """THE default that matters. A rule created without an explicit mode must
    not be able to move anything — 'test it in production on a real gate' is
    not an acceptable validation strategy."""
    rid = _insert_rule()
    row = _run(_query("SELECT * FROM alert_rules WHERE id = $1", rid))[0]
    assert row["mode"] == "log_only"
    assert row["enabled"] is True
    assert row["cooldown_s"] == 60
    assert row["max_event_age_s"] == 30
    assert row["severity"] == "info"


def test_name_is_unique_case_insensitively(migrated):
    _insert_rule(name="Gate Rule")
    with pytest.raises(asyncpg.UniqueViolationError):
        _insert_rule(name="gate rule")


def test_mode_is_constrained_by_the_database(migrated):
    with pytest.raises(asyncpg.CheckViolationError):
        _insert_rule(name="bad mode", mode="armed_ish")


def test_scope_kind_is_constrained_by_the_database(migrated):
    with pytest.raises(asyncpg.CheckViolationError):
        _insert_rule(name="bad scope", scope_kind="planet")


def test_a_scoped_rule_must_name_its_scope(migrated):
    """A camera-scoped rule with no camera would match nothing and still look
    configured in the list. The database refuses it."""
    with pytest.raises(asyncpg.CheckViolationError):
        _insert_rule(name="scoped but empty", scope_kind="camera")


def test_an_any_scoped_rule_must_not_name_one(migrated):
    with pytest.raises(asyncpg.CheckViolationError):
        _insert_rule(name="any but named", scope_kind="any", scope_value="cam-1")


def test_deleting_a_rule_keeps_the_alert_and_nulls_the_fk(migrated):
    """ON DELETE SET NULL, not CASCADE. alerts.rule_name is denormalised so a
    historical alert still reads correctly after its rule is gone. Deleting a
    rule must never delete evidence."""
    rid = _insert_rule(name="doomed")
    cam = _a_camera()
    aid = uuid.uuid4()
    _run(_execute(
        "INSERT INTO alerts (id, rule_id, rule_name, camera_id, triggered_at) "
        "VALUES ($1, $2, 'doomed', $3, now())", aid, rid, cam))

    _run(_execute("DELETE FROM alert_rules WHERE id = $1", rid))

    rows = _run(_query("SELECT rule_id, rule_name FROM alerts WHERE id = $1", aid))
    assert len(rows) == 1, "the alert was deleted with its rule — evidence lost"
    assert rows[0]["rule_id"] is None
    assert rows[0]["rule_name"] == "doomed"


def test_the_hot_lookup_index_exists(migrated):
    """The engine reads enabled rules by activity on every ingested event."""
    rows = _run(_query(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_alert_rules_activity'"))
    assert rows, "ix_alert_rules_activity is missing"
    assert "enabled" in rows[0]["indexdef"], "the partial WHERE enabled was dropped"


def test_downgrade_is_clean(migrated):
    """039 must be reversible: an operator who rolls back should not be left
    with a dangling FK on alerts."""
    command.downgrade(_cfg(), "038")
    tables = _run(_query(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'alert_rules'"))
    assert not tables
    fks = _run(_query(
        "SELECT 1 FROM information_schema.table_constraints "
        "WHERE constraint_name = 'fk_alerts_rule'"))
    assert not fks
    command.upgrade(_cfg(), "039")          # leave the database where we found it


# ── The engine against a real database ────────────────────────────────────────
#
# test_rules_engine_fire.py pins the DECISIONS against a session that only
# records what it was asked to store. These pin that a decision becomes a row:
# the FK resolves, the uuid[] of cited events round-trips, and the JSONB
# outcome map survives. None of that is provable without Postgres.

def _session(fn):
    """Run `fn(session)` on a fresh engine — an asyncpg pool belongs to one loop."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    async def go():
        engine = create_async_engine(_alembic_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _stub_valkey(monkeypatch):
    """The engine reads the arm state and the cooldown from Valkey. These tests
    are about the DATABASE, so give it an in-memory one."""
    from backend.services import rules_engine

    class Mem:
        def __init__(self):
            self.store = {}

        async def get(self, key):
            return self.store.get(key)

        async def set(self, key, value, ex=None, nx=False, **kw):
            if nx and key in self.store:
                return None
            self.store[key] = value
            return True

        async def incr(self, key):
            self.store[key] = int(self.store.get(key, 0)) + 1
            return self.store[key]

    mem = Mem()

    async def _get():
        return mem

    monkeypatch.setattr(rules_engine, "get_redis", _get)
    rules_engine._local_cooldown.clear()
    rules_engine._cache["version"] = None
    rules_engine._cache["rules"] = []
    return mem


def _evaluate(events, now=None):
    from datetime import datetime, timezone

    from backend.services import rules_engine

    async def go(s):
        return await rules_engine.evaluate(
            s, events, now=now or datetime.now(timezone.utc))

    return _session(go)


def _event_for(cam, **kw):
    from datetime import datetime, timezone
    base = {"id": uuid.uuid4(), "camera_id": cam, "camera_slug": "cam-x",
            "activity": "unauthorised_access", "zone": "Zone 1", "confidence": 0.9,
            "object_class": "person", "attributes": {},
            "started_at": datetime.now(timezone.utc), "dedupe_key": None}
    base.update(kw)
    return base


def test_a_fired_rule_writes_a_real_alert_row(migrated, monkeypatch):
    """The first end-to-end proof: a detection becomes a persisted alert whose
    FK to its rule resolves."""
    _stub_valkey(monkeypatch)
    rid = _insert_rule(name="gate on intrusion", mode="armed",
                       severity="warning", cooldown_s=0)
    cam = _a_camera()
    ev = _event_for(cam)

    ids = _evaluate([ev])
    assert len(ids) == 1

    [row] = _run(_query("SELECT * FROM alerts WHERE id = $1", ids[0]))
    assert row["rule_id"] == rid
    assert row["rule_name"] == "gate on intrusion"
    assert row["camera_id"] == cam
    assert row["severity"] == "warning"
    assert row["state"] == "new"
    assert row["legal_hold"] is False


def test_the_alert_cites_its_event_in_the_uuid_array(migrated, monkeypatch):
    """alerts.event_ids is a uuid[], the join that makes the DPDP custody chain
    resolvable: audit_log -> alerts.event_ids -> analytics_events -> camera."""
    _stub_valkey(monkeypatch)
    _insert_rule(name="cites", mode="armed", cooldown_s=0)
    cam = _a_camera()
    ev = _event_for(cam)

    [aid] = _evaluate([ev])
    [row] = _run(_query("SELECT event_ids FROM alerts WHERE id = $1", aid))
    assert row["event_ids"] == [ev["id"]]


def test_the_outcome_map_round_trips_as_jsonb(migrated, monkeypatch):
    """The outcome map survives the JSONB round trip, carrying whatever the
    bridge actually answered.

    In P1 that answer was the constant `no_bridge`. Since P2 it is the bridge's
    own: here `unknown_peripheral`, because this scratch database has a rule
    naming a device that is not in its inventory — which is the boundary rule
    doing its job, and still not a claim that the barrier moved. THAT is the
    property this test has always been protecting."""
    import json
    _stub_valkey(monkeypatch)
    _insert_rule(
        name="with actions", mode="armed", cooldown_s=0,
        actions=json.dumps([{"type": "peripheral", "peripheral": "gate-3-barrier",
                             "command": "open"}]))
    cam = _a_camera()

    [aid] = _evaluate([_event_for(cam)])
    [row] = _run(_query("SELECT notified FROM alerts WHERE id = $1", aid))
    notified = row["notified"]
    notified = json.loads(notified) if isinstance(notified, str) else notified
    assert notified["gate-3-barrier"] == "unknown_peripheral"
    assert "no_bridge" not in str(notified), "P1's placeholder is retired"


def test_a_disabled_rule_writes_nothing(migrated, monkeypatch):
    """The cache loads `WHERE enabled`; a disabled rule must never reach the
    gate chain at all."""
    _stub_valkey(monkeypatch)
    _insert_rule(name="off", mode="armed", enabled=False, cooldown_s=0)
    cam = _a_camera()

    assert _evaluate([_event_for(cam)]) == []
    assert _run(_query("SELECT count(*) AS n FROM alerts"))[0]["n"] == 0


def test_a_burst_collapses_to_one_row_in_the_table(migrated, monkeypatch):
    """The cooldown is what stops a busy tripwire filling the table."""
    _stub_valkey(monkeypatch)
    _insert_rule(name="collapsing", mode="armed", cooldown_s=60)
    cam = _a_camera()

    ids = _evaluate([_event_for(cam) for _ in range(25)])
    assert len(ids) == 1
    assert _run(_query("SELECT count(*) AS n FROM alerts"))[0]["n"] == 1


# ── The router against a real database ───────────────────────────────────────
# test_rules_http.py drives these endpoints over ASGI and asserts what the ASGI
# layer owns: status codes, validation, authorisation and the audit call. What
# it cannot reach is anything needing a SELECT or a database constraint, because
# its session is a stub. These three are exactly that residue — the handlers
# called directly, with a real session underneath.

def _principal(subject="alice"):
    from backend.security import Principal
    return Principal(subject=subject, roles=frozenset({"admin"}), kind="user")


def _quiet_audit(monkeypatch, module=None):
    """The handlers audit through their own module attribute; audit.record
    opens its OWN session from settings.database_url, which is the appliance's
    database, not this scratch one. Capture instead."""
    if module is None:
        from backend.routers import rules as module

    seen = []

    async def record(request, principal, action, *, target=None, detail=None,
                     outcome="success"):
        seen.append(action)

    monkeypatch.setattr(module.audit_svc, "record", record)
    return seen


def _call(fn, **kw):
    async def go(s):
        return await fn(db=s, **kw)
    return _session(go)


def test_a_duplicate_name_is_refused_through_the_router(migrated, monkeypatch):
    """Case-insensitive, and this is the half the contract suite cannot prove:
    the pre-check is a SELECT and the real guard is ix_alert_rules_name_lower.
    A stub session answers neither."""
    from fastapi import HTTPException

    from backend.models import AlertRuleIn
    from backend.routers import rules as rules_router
    _stub_valkey(monkeypatch)
    _quiet_audit(monkeypatch)

    body = AlertRuleIn(name="Gate Rule", activity="unauthorised_access")
    _call(rules_router.create_rule, body=body, request=None, principal=_principal())

    with pytest.raises(HTTPException) as caught:
        _call(rules_router.create_rule,
              body=AlertRuleIn(name="gate rule", activity="unauthorised_access"),
              request=None, principal=_principal())
    assert caught.value.status_code == 409
    assert _run(_query("SELECT count(*) AS n FROM alert_rules"))[0]["n"] == 1


def test_arming_through_the_router_reaches_the_table(migrated, monkeypatch):
    """A rule the engine will actually read is one whose `mode` column moved.
    The contract test proves the response says "armed"; this proves the row
    does, which is what `_load_rules` selects on."""
    from backend.models import AlertRuleIn
    from backend.routers import rules as rules_router
    _stub_valkey(monkeypatch)
    audited = _quiet_audit(monkeypatch)

    created = _call(rules_router.create_rule,
                    body=AlertRuleIn(name="gate on intrusion",
                                     activity="unauthorised_access"),
                    request=None, principal=_principal())
    [row] = _run(_query("SELECT mode FROM alert_rules WHERE id = $1", created.id))
    assert row["mode"] == "log_only"

    _call(rules_router.arm_rule, rule_id=created.id, request=None,
          principal=_principal())
    [row] = _run(_query("SELECT mode FROM alert_rules WHERE id = $1", created.id))
    assert row["mode"] == "armed"
    assert audited == ["rule.created", "rule.armed"]


def test_deleting_through_the_router_keeps_the_alert(migrated, monkeypatch):
    """The endpoint, not just the FK. `test_deleting_a_rule_keeps_the_alert_and_
    nulls_the_fk` proves the schema behaves; this proves the handler goes
    through it rather than around it — deleting a rule must never delete
    evidence."""
    from backend.models import AlertRuleIn
    from backend.routers import rules as rules_router
    _stub_valkey(monkeypatch)
    _quiet_audit(monkeypatch)

    created = _call(rules_router.create_rule,
                    body=AlertRuleIn(name="doomed", activity="unauthorised_access"),
                    request=None, principal=_principal())
    cam = _a_camera()
    aid = uuid.uuid4()
    _run(_execute(
        "INSERT INTO alerts (id, rule_id, rule_name, camera_id, triggered_at) "
        "VALUES ($1, $2, $3, $4, now())", aid, created.id, "doomed", cam))

    _call(rules_router.delete_rule, rule_id=created.id, request=None,
          principal=_principal())

    assert not _run(_query("SELECT 1 FROM alert_rules WHERE id = $1", created.id))
    [row] = _run(_query("SELECT rule_id, rule_name FROM alerts WHERE id = $1", aid))
    assert row["rule_id"] is None
    assert row["rule_name"] == "doomed"


# ── The alert state machine and the retention sweep ──────────────────────────
# test_alerts_http.py proves who may call these and what an unknown id does.
# What it cannot prove is that a call leaves a mark: its session holds no
# alerts, because nothing but the engine creates one. These do.

def _an_alert(cam, **cols):
    aid = uuid.uuid4()
    keys = ", ".join(cols)
    holes = ", ".join(f"${i + 4}" for i in range(len(cols)))
    tail = f", {keys}" if cols else ""
    vals = f", {holes}" if cols else ""
    _run(_execute(
        f"INSERT INTO alerts (id, rule_name, camera_id, triggered_at{tail}) "
        f"VALUES ($1, $2, $3, now(){vals})",
        aid, "a rule", cam, *cols.values()))
    return aid


def test_ack_stamps_who_and_when(migrated, monkeypatch):
    """Who acknowledged it and when are the two facts an operator handover
    needs, and neither can be reconstructed afterwards."""
    from backend.routers import alerts as alerts_router
    _quiet_audit(monkeypatch, alerts_router)
    aid = _an_alert(_a_camera())

    _call(alerts_router.acknowledge, alert_id=aid, request=None,
          principal=_principal("alice"))

    [row] = _run(_query("SELECT state, acknowledged_by, acknowledged_at "
                        "FROM alerts WHERE id = $1", aid))
    assert row["state"] == "acknowledged"
    assert row["acknowledged_by"] == "alice"
    assert row["acknowledged_at"] is not None


def test_resolving_records_the_verdict_that_reporting_groups_on(migrated,
                                                                monkeypatch):
    from backend.routers import alerts as alerts_router
    from backend.routers.alerts import ResolveBody
    _quiet_audit(monkeypatch, alerts_router)
    aid = _an_alert(_a_camera())

    _call(alerts_router.resolve, alert_id=aid,
          body=ResolveBody(resolution="false_positive", note="a moth"),
          request=None, principal=_principal("bob"))

    [row] = _run(_query("SELECT state, resolution, resolution_note, resolved_by "
                        "FROM alerts WHERE id = $1", aid))
    assert row["state"] == "resolved"
    assert row["resolution"] == "false_positive"
    assert row["resolution_note"] == "a moth"
    assert row["resolved_by"] == "bob"


def test_legal_hold_blocks_the_retention_sweep(migrated, monkeypatch):
    """An alert on legal hold survives its own expiry. The one retention rule
    in this product that is not negotiable."""
    from backend.services import rules_engine
    cam = _a_camera()
    old = _an_alert(cam)
    held = _an_alert(cam)
    _run(_execute("UPDATE alerts SET triggered_at = now() - interval '400 days'"))
    _run(_execute("UPDATE alerts SET legal_hold = true WHERE id = $1", held))

    removed = _session(lambda s: rules_engine.purge_expired_alerts(s, keep_days=365))
    assert removed == 1
    assert _run(_query("SELECT 1 FROM alerts WHERE id = $1", held))
    assert not _run(_query("SELECT 1 FROM alerts WHERE id = $1", old))


def test_a_resolved_alert_is_not_swept_by_age_alone(migrated, monkeypatch):
    """Resolved alerts are the record of what operators actually decided.
    Sweeping them by age would delete the false-positive denominator."""
    from backend.services import rules_engine
    aid = _an_alert(_a_camera())
    _run(_execute("UPDATE alerts SET triggered_at = now() - interval '400 days', "
                  "resolved_at = now(), resolution = 'false_positive' "
                  "WHERE id = $1", aid))

    assert _session(
        lambda s: rules_engine.purge_expired_alerts(s, keep_days=365)) == 0
    assert _run(_query("SELECT 1 FROM alerts WHERE id = $1", aid))


def test_the_sweep_leaves_everything_inside_the_window(migrated, monkeypatch):
    """The other half of the sweep: it must not be a truncate that happens to
    spare two rows."""
    from backend.services import rules_engine
    cam = _a_camera()
    fresh = _an_alert(cam)
    stale = _an_alert(cam)
    _run(_execute("UPDATE alerts SET triggered_at = now() - interval '400 days' "
                  "WHERE id = $1", stale))

    assert _session(
        lambda s: rules_engine.purge_expired_alerts(s, keep_days=365)) == 1
    assert _run(_query("SELECT 1 FROM alerts WHERE id = $1", fresh))


def test_an_evidence_hold_placed_through_the_router_is_what_the_sweep_reads(
        migrated, monkeypatch):
    """The endpoint and the sweep have to agree about one column. They are in
    different modules and nothing but this joins them up."""
    from backend.routers import alerts as alerts_router
    from backend.routers.alerts import HoldBody
    from backend.services import rules_engine
    _quiet_audit(monkeypatch, alerts_router)
    aid = _an_alert(_a_camera())

    _call(alerts_router.set_hold, alert_id=aid, body=HoldBody(legal_hold=True),
          request=None, principal=_principal())
    _run(_execute("UPDATE alerts SET triggered_at = now() - interval '400 days'"))

    assert _session(
        lambda s: rules_engine.purge_expired_alerts(s, keep_days=365)) == 0
    assert _run(_query("SELECT 1 FROM alerts WHERE id = $1", aid))
