"""The taxonomy against a real database — everything the StubSession cannot say.

    ANNOTATIONS_TEST_DB=postgresql+asyncpg://user:pw@host:5432/scratch \
        python -m pytest services/camera-mgmt/tests/test_label_classes_db.py

Routers are called DIRECTLY on a real session (the `_call` shape
test_alert_rules_db.py uses), because what is under test is the SELECT — the
annotation counts, and the delete-vs-retire decision the FK enforces.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest

DB_URL = os.environ.get("ANNOTATIONS_TEST_DB")

pytestmark = pytest.mark.skipif(
    not DB_URL, reason="set ANNOTATIONS_TEST_DB to a scratch database URL")

if DB_URL:
    pytest.importorskip("alembic", reason="alembic lives in the api image")
    asyncpg = pytest.importorskip("asyncpg")
    from alembic import command                               # noqa: E402
    from alembic.config import Config                         # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _alembic_url() -> str:
    return DB_URL if "+asyncpg" in DB_URL else DB_URL.replace(
        "postgresql://", "postgresql+asyncpg://")


def _asyncpg_dsn() -> str:
    return _alembic_url().replace("+asyncpg", "")


def _cfg() -> "Config":
    os.environ["DATABASE_URL"] = _alembic_url()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", _alembic_url())
    return cfg


def _run(coro):
    return asyncio.run(coro)


async def _execute(sql: str, *args):
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        return await conn.execute(sql, *args)
    finally:
        await conn.close()


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


def _call(fn, **kw):
    async def go(s):
        return await fn(db=s, **kw)
    return _session(go)


def _principal(subject="alice"):
    from backend.security import Principal
    return Principal(subject=subject, roles=frozenset({"admin"}), kind="user")


@pytest.fixture(scope="module")
def migrated():
    async def _count():
        conn = await asyncpg.connect(_asyncpg_dsn())
        try:
            return await conn.fetchval(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public'")
        finally:
            await conn.close()
    if _run(_count()):
        pytest.skip("ANNOTATIONS_TEST_DB is not empty; refusing to migrate it")
    command.upgrade(_cfg(), "048")
    yield


@pytest.fixture(autouse=True)
def quiet_audit(migrated, monkeypatch):
    """The handlers audit through their own module attribute; a real record
    would need a request object these direct calls do not have."""
    from backend.routers import label_classes as router_mod

    async def _record(*a, **kw):
        recorded.append((a, kw))
    recorded: list = []
    monkeypatch.setattr(router_mod.audit_svc, "record", _record)
    yield recorded
    _run(_execute("DELETE FROM annotations"))
    _run(_execute("DELETE FROM label_classes"))


def _seed_annotation(class_key: str) -> uuid.UUID:
    aid = uuid.uuid4()
    _run(_execute(
        "INSERT INTO annotations (id, camera_slug, class_key, captured_at,"
        " bbox_x, bbox_y, bbox_w, bbox_h, frame_width, frame_height, image_path)"
        " VALUES ($1,'gate-cam',$2, now(), 10, 20, 30, 40, 1920, 1080, '/a.jpg')",
        aid, class_key))
    return aid


def _create(key="forklift", label=None, color="#ff8800"):
    from backend.models import LabelClassCreateBody
    from backend.routers import label_classes as r
    return _call(r.create_class,
                 body=LabelClassCreateBody(key=key, label=label or key.title(),
                                           color=color),
                 request=None, principal=_principal())


def test_a_created_class_comes_back_in_the_listing(migrated):
    from backend.routers import label_classes as r
    _create("forklift")
    _create("pallet")
    rows = _call(r.list_classes)
    assert [c.key for c in rows] == ["forklift", "pallet"]
    assert all(c.active for c in rows)


def test_the_listing_carries_the_annotation_count(migrated):
    """The count is the number that says which class is under-sampled, which
    is the whole reason to have a taxonomy rather than free text."""
    from backend.routers import label_classes as r
    _create("forklift")
    _create("pallet")
    _seed_annotation("forklift")
    _seed_annotation("forklift")
    counts = {c.key: c.annotation_count for c in _call(r.list_classes)}
    assert counts == {"forklift": 2, "pallet": 0}


def test_a_duplicate_key_is_refused(migrated):
    from fastapi import HTTPException
    _create("forklift")
    with pytest.raises(HTTPException) as exc:
        _create("forklift")
    assert exc.value.status_code == 409


def test_deleting_a_class_in_use_is_refused_and_says_retire(migrated):
    from fastapi import HTTPException
    from backend.routers import label_classes as r
    _create("forklift")
    _seed_annotation("forklift")
    with pytest.raises(HTTPException) as exc:
        _call(r.delete_class, key="forklift", request=None,
              principal=_principal())
    assert exc.value.status_code == 409
    assert "retire" in str(exc.value.detail).lower()


def test_an_unused_class_can_be_deleted(migrated):
    from backend.routers import label_classes as r
    _create("unused")
    _call(r.delete_class, key="unused", request=None, principal=_principal())
    assert _call(r.list_classes) == []


def test_retiring_keeps_the_row_and_flips_active(migrated):
    from backend.models import LabelClassUpdateBody
    from backend.routers import label_classes as r
    _create("forklift")
    _seed_annotation("forklift")
    out = _call(r.update_class, key="forklift",
                body=LabelClassUpdateBody(active=False),
                request=None, principal=_principal())
    assert out.active is False
    assert out.annotation_count == 1


def test_retiring_audits_as_retired_not_updated(migrated, quiet_audit):
    from backend.models import LabelClassUpdateBody
    from backend.routers import label_classes as r
    _create("forklift")
    _call(r.update_class, key="forklift",
          body=LabelClassUpdateBody(active=False),
          request=None, principal=_principal())
    actions = [a[2] for a, _ in quiet_audit if len(a) > 2]
    assert "label_class.retired" in actions


def test_the_label_studio_config_renders_active_classes_only(migrated):
    from backend.models import LabelClassUpdateBody
    from backend.routers import label_classes as r
    _create("forklift")
    _create("old")
    _call(r.update_class, key="old", body=LabelClassUpdateBody(active=False),
          request=None, principal=_principal())
    out = _call(r.label_config)
    assert '<Label value="forklift" background="#ff8800"/>' in out["xml"]
    assert "old" not in out["xml"]
    assert out["class_count"] == 1


def test_the_label_studio_config_is_wellformed_xml(migrated):
    """It is pasted into somebody else's tool. A config that does not parse is
    a support ticket, not a feature."""
    from xml.etree import ElementTree
    from backend.routers import label_classes as r
    _create("forklift")
    root = ElementTree.fromstring(_call(r.label_config)["xml"])
    assert root.tag == "View"
    assert [e.get("value") for e in root.iter("Label")] == ["forklift"]
