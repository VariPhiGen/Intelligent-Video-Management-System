"""What reaches the `annotations` table, against a real database.

    ANNOTATIONS_TEST_DB=postgresql+asyncpg://user:pw@host:5432/scratch \
        python -m pytest services/camera-mgmt/tests/test_annotations_router_db.py

The contract suite proves the edge refuses what it should. This proves the row
that lands is the row the sidecar describes — and that the file-before-row
ordering holds in both directions.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
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
CAMERA = "gate-cam"
EPOCH = 1790000000.0


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


async def _query(sql, *args):
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def _execute(sql, *args):
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        return await conn.execute(sql, *args)
    finally:
        await conn.close()


def _session(fn):
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


class _Upload:
    filename = "annotation.jpg"

    async def read(self):
        return b"\xff\xd8\xff\xdb" + b"0" * 64


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
def wired(migrated, monkeypatch):
    """Quiet the audit (no request object here) and stand in for the NVR."""
    from backend.routers import annotations as router_mod

    async def _record(*a, **kw):
        return None
    monkeypatch.setattr(router_mod.audit_svc, "record", _record)

    state = {"deleted": [], "saved": 0, "delete_ok": True}

    async def _save(form, files):
        state["saved"] += 1
        aid = uuid.uuid4().hex
        return {"id": aid, "camera": form["camera"],
                "class_key": form["class_key"],
                "timestamp": form["timestamp"], "timestamp_epoch": EPOCH,
                "bbox": {"x": 10.0, "y": 20.0, "width": 30.0, "height": 40.0},
                "frame": {"width": 1920, "height": 1080},
                "image_path": f"/data/nvr/annotations/{CAMERA}/{int(EPOCH)}_{aid}.jpg"}

    async def _delete(annotation_id):
        if not state["delete_ok"]:
            return False
        state["deleted"].append(annotation_id)
        return True

    monkeypatch.setattr(router_mod.nvr_client, "save_annotation", _save)
    monkeypatch.setattr(router_mod.nvr_client, "delete_annotation", _delete)
    _run(_execute(
        "INSERT INTO label_classes (key, label, color, sort_order, active)"
        " VALUES ('forklift','Forklift','#ff8800',0,true)"))
    yield state
    _run(_execute("DELETE FROM annotations"))
    _run(_execute("DELETE FROM label_classes"))
    _run(_execute("DELETE FROM datasets"))


def _create(**over):
    from backend.routers import annotations as r
    kw = dict(request=None, camera=CAMERA,
              timestamp="2026-09-25T10:00:00+00:00", class_key="forklift",
              bbox_x=10.0, bbox_y=20.0, bbox_w=30.0, bbox_h=40.0,
              frame_width=1920, frame_height=1080, dataset_id=None,
              image=_Upload(), principal=_principal())
    kw.update(over)
    return _call(r.create_annotation, **kw)


def _a_dataset() -> int:
    rows = _run(_query(
        "INSERT INTO datasets (created_at, updated_at, name, camera_slug,"
        " window_start, window_end, target_frames)"
        " VALUES (now(), now(), 'd', $1, now() - interval '1 hour', now(), 10)"
        " RETURNING id", CAMERA))
    return rows[0]["id"]


def test_the_row_matches_what_the_nvr_reported(migrated):
    out = _create()
    rows = _run(_query("SELECT * FROM annotations WHERE id = $1",
                       uuid.UUID(out.id)))
    assert len(rows) == 1
    row = rows[0]
    assert row["camera_slug"] == CAMERA
    assert row["class_key"] == "forklift"
    # Source-video pixels, exactly as drawn — not display pixels.
    assert (row["bbox_x"], row["bbox_y"], row["bbox_w"], row["bbox_h"]) == \
        (10.0, 20.0, 30.0, 40.0)
    assert (row["frame_width"], row["frame_height"]) == (1920, 1080)
    assert row["created_by"] == "alice"
    assert row["image_path"].endswith(".jpg")
    assert row["mask_path"] is None       # reserved; nothing in the VMS writes it


def test_captured_at_comes_from_the_nvr_not_the_request(migrated):
    """The NVR stamped the frame; the request's own clock is not authoritative."""
    out = _create()
    rows = _run(_query("SELECT captured_at FROM annotations WHERE id = $1",
                       uuid.UUID(out.id)))
    assert rows[0]["captured_at"] == datetime.fromtimestamp(
        EPOCH, tz=timezone.utc)


def test_a_loose_annotation_is_stamped_with_an_expiry(migrated):
    out = _create()
    rows = _run(_query("SELECT captured_at, expires_at FROM annotations"
                       " WHERE id = $1", uuid.UUID(out.id)))
    from backend.config import settings
    expected = rows[0]["captured_at"] + timedelta(
        minutes=settings.annotation_ttl_minutes)
    assert rows[0]["expires_at"] == expected


def test_a_dataset_attached_annotation_is_exempt_from_expiry(migrated):
    did = _a_dataset()
    out = _create(dataset_id=did)
    rows = _run(_query("SELECT dataset_id, expires_at FROM annotations"
                       " WHERE id = $1", uuid.UUID(out.id)))
    assert rows[0]["dataset_id"] == did
    assert rows[0]["expires_at"] is None


def test_delete_removes_the_row_only_after_the_file(migrated, wired):
    from backend.routers import annotations as r
    out = _create()
    _call(r.delete_annotation, annotation_id=uuid.UUID(out.id), request=None,
          principal=_principal())
    assert wired["deleted"] == [uuid.UUID(out.id).hex]
    assert _run(_query("SELECT id FROM annotations")) == []


def test_a_failed_file_delete_keeps_the_row(migrated, wired):
    """Otherwise the orphaned file is handed straight back to the adoption
    pass, which would re-index exactly what we tried to remove."""
    from fastapi import HTTPException
    from backend.routers import annotations as r
    out = _create()
    wired["delete_ok"] = False
    with pytest.raises(HTTPException) as exc:
        _call(r.delete_annotation, annotation_id=uuid.UUID(out.id),
              request=None, principal=_principal())
    assert exc.value.status_code == 502
    assert len(_run(_query("SELECT id FROM annotations"))) == 1


def test_an_unknown_class_never_reaches_the_nvr(migrated, wired):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _create(class_key="nosuchclass")
    assert exc.value.status_code == 422
    assert wired["saved"] == 0


# ── The read surface ────────────────────────────────────────────────────────
# Filters and counts are SELECTs, so they cannot be proven on the StubSession.


def _seed(class_key="forklift", camera=CAMERA, captured=None, dataset_id=None):
    aid = uuid.uuid4()
    _run(_execute(
        "INSERT INTO annotations (id, camera_slug, class_key, captured_at,"
        " bbox_x, bbox_y, bbox_w, bbox_h, frame_width, frame_height,"
        " image_path, dataset_id)"
        " VALUES ($1,$2,$3,$4,10,20,30,40,1920,1080,'/a.jpg',$5)",
        aid, camera, class_key,
        captured or datetime.now(timezone.utc), dataset_id))
    return aid


def _extra_class(key):
    _run(_execute(
        "INSERT INTO label_classes (key, label, color, sort_order, active)"
        " VALUES ($1, $2, '#00aa88', 1, true)", key, key.title()))


def test_stats_counts_each_class(migrated):
    from backend.routers import annotations as r
    _extra_class("pallet")
    _seed("forklift"); _seed("forklift"); _seed("pallet")
    out = _call(r.annotation_stats, principal=_principal())
    assert out["by_class"] == {"forklift": 2, "pallet": 1}
    assert out["total"] == 3


def test_stats_on_an_empty_pile_is_zero_not_an_error(migrated):
    from backend.routers import annotations as r
    out = _call(r.annotation_stats, principal=_principal())
    assert out == {"by_class": {}, "by_camera": {}, "total": 0}


def test_stats_counts_each_camera_that_has_annotations(migrated):
    """What the camera filter is built from.

    Not the camera registry: 047 keeps camera_slug out of a foreign key so an
    annotation outlives its camera, so the registry and this set diverge on
    purpose. A picker built from the registry would offer cameras with nothing
    and hide a decommissioned one whose samples are still here.
    """
    from backend.routers import annotations as r
    _seed(camera="gate-cam"); _seed(camera="gate-cam")
    _seed(camera="dock-cam")
    out = _call(r.annotation_stats, principal=_principal())
    assert out["by_camera"] == {"dock-cam": 1, "gate-cam": 2}


def test_stats_still_counts_a_camera_the_registry_has_forgotten(migrated):
    """The case the FK decision protects, stated where a picker can see it."""
    from backend.routers import annotations as r
    _seed(camera="decommissioned-cam")
    out = _call(r.annotation_stats, principal=_principal())
    assert out["by_camera"] == {"decommissioned-cam": 1}


def test_listing_filters_by_class(migrated):
    from backend.routers import annotations as r
    _extra_class("pallet")
    _seed("forklift"); _seed("pallet")
    out = _call(r.list_annotations, class_key="forklift",
                principal=_principal())
    assert out["total"] == 1
    assert out["annotations"][0].class_key == "forklift"


def test_listing_filters_by_camera(migrated):
    from backend.routers import annotations as r
    _seed(camera=CAMERA)
    _seed(camera="yard-cam")
    out = _call(r.list_annotations, camera="yard-cam", principal=_principal())
    assert out["total"] == 1
    assert out["annotations"][0].camera_slug == "yard-cam"


def test_listing_filters_by_window(migrated):
    from backend.routers import annotations as r
    now = datetime.now(timezone.utc)
    _seed(captured=now)
    _seed(captured=now - timedelta(days=5))
    out = _call(r.list_annotations, from_=now - timedelta(hours=1),
                principal=_principal())
    assert out["total"] == 1


def test_listing_is_newest_first(migrated):
    from backend.routers import annotations as r
    now = datetime.now(timezone.utc)
    older = _seed(captured=now - timedelta(days=1))
    newer = _seed(captured=now)
    out = _call(r.list_annotations, principal=_principal())
    assert [a.id for a in out["annotations"]] == [newer.hex, older.hex]


def test_total_counts_the_whole_match_not_the_page(migrated):
    """Otherwise the UI reports '5 of 5' while 40 more sit behind the limit."""
    from backend.routers import annotations as r
    for _ in range(5):
        _seed()
    out = _call(r.list_annotations, limit=2, principal=_principal())
    assert len(out["annotations"]) == 2
    assert out["total"] == 5


# ── COCO export ─────────────────────────────────────────────────────────────


def _coco(**kw):
    from backend.services import annotation_export

    async def go(session):
        return await annotation_export.build_coco(session, **kw)
    return _session(go)


def test_categories_are_the_taxonomy(migrated):
    _extra_class("pallet")
    _seed("forklift")
    doc = _coco()
    assert [c["name"] for c in doc["categories"]] == ["forklift", "pallet"]
    # 1-based: a 0 id reads as "no category" to several loaders.
    assert min(c["id"] for c in doc["categories"]) == 1


def test_bbox_is_coco_xywh_in_source_pixels(migrated):
    _seed("forklift")
    doc = _coco()
    assert doc["annotations"][0]["bbox"] == [10.0, 20.0, 30.0, 40.0]
    assert doc["annotations"][0]["area"] == 1200.0
    assert doc["images"][0]["width"] == 1920
    assert doc["images"][0]["height"] == 1080


def test_every_annotation_points_at_a_category_in_the_file(migrated):
    """A retired class must still export its category, or its annotations
    would reference an id the file does not define."""
    _extra_class("retired_one")
    _seed("retired_one")
    _run(_execute(
        "UPDATE label_classes SET active = false WHERE key = 'retired_one'"))
    doc = _coco()
    defined = {c["id"] for c in doc["categories"]}
    assert defined
    assert all(a["category_id"] in defined for a in doc["annotations"])


def test_the_export_can_be_narrowed_by_class(migrated):
    _extra_class("pallet")
    _seed("forklift")
    _seed("pallet")
    doc = _coco(class_keys=["forklift"])
    assert len(doc["annotations"]) == 1
    # Categories stay complete even when annotations are filtered — the file
    # still has to define what the ids mean.
    assert len(doc["categories"]) == 2


def test_image_ids_and_annotation_ids_line_up(migrated):
    for _ in range(3):
        _seed("forklift")
    doc = _coco()
    assert [i["id"] for i in doc["images"]] == [1, 2, 3]
    assert [a["image_id"] for a in doc["annotations"]] == [1, 2, 3]


def test_provenance_travels_with_each_image(migrated):
    aid = _seed("forklift")
    doc = _coco()
    assert doc["images"][0]["camera_slug"] == CAMERA
    assert doc["images"][0]["annotation_id"] == aid.hex


def test_an_empty_pile_still_produces_a_valid_document(migrated):
    doc = _coco()
    assert doc["annotations"] == [] and doc["images"] == []
    assert "info" in doc and "categories" in doc


# ── Relabel and keep, persisted ─────────────────────────────────────────────


def _patch(aid, **fields):
    from backend.models import AnnotationUpdateBody
    from backend.routers import annotations as r
    return _call(r.update_annotation, annotation_id=aid,
                 body=AnnotationUpdateBody(**fields), request=None,
                 principal=_principal())


def test_a_relabel_reaches_the_table(migrated):
    _extra_class("pallet")
    aid = _seed("forklift")
    _patch(aid, class_key="pallet")
    rows = _run(_query("SELECT class_key FROM annotations WHERE id = $1", aid))
    assert rows[0]["class_key"] == "pallet"


def test_a_relabel_does_not_disturb_the_frame_or_the_box(migrated):
    """Fixing the label must not touch the evidence it labels."""
    _extra_class("pallet")
    aid = _seed("forklift")
    before = _run(_query("SELECT image_path, bbox_x, bbox_w, captured_at"
                         " FROM annotations WHERE id = $1", aid))[0]
    _patch(aid, class_key="pallet")
    after = _run(_query("SELECT image_path, bbox_x, bbox_w, captured_at"
                        " FROM annotations WHERE id = $1", aid))[0]
    assert dict(before) == dict(after)


def test_keeping_clears_the_deadline_and_records_why(migrated):
    aid = _seed("forklift")
    _run(_execute("UPDATE annotations SET expires_at = now() + interval '1 day'"
                  " WHERE id = $1", aid))
    _patch(aid, keep=True, kept_reason="forklift training set")
    rows = _run(_query("SELECT expires_at, kept_reason FROM annotations"
                       " WHERE id = $1", aid))
    assert rows[0]["expires_at"] is None
    assert rows[0]["kept_reason"] == "forklift training set"


def test_a_kept_annotation_survives_the_sweep(migrated):
    """The whole point: before 048 every annotation was swept because nothing
    could reach the exemption."""
    from backend.services import annotation_store
    aid = _seed("forklift")
    _run(_execute("UPDATE annotations SET expires_at = now() - interval '1 day'"
                  " WHERE id = $1", aid))
    _patch(aid, keep=True, kept_reason="evidence of the near miss")

    async def go(s):
        return await annotation_store.purge_expired(s)
    assert _session(go) == 0
    assert len(_run(_query("SELECT id FROM annotations WHERE id = $1", aid))) == 1


def test_releasing_re_bases_on_capture_not_on_now(migrated):
    """Release must not silently extend the life of an old frame."""
    from backend.config import settings
    aid = _seed("forklift")
    _patch(aid, keep=True, kept_reason="temporary")
    _patch(aid, keep=False)
    rows = _run(_query("SELECT captured_at, expires_at, kept_reason"
                       " FROM annotations WHERE id = $1", aid))[0]
    assert rows["kept_reason"] is None
    delta = (rows["expires_at"] - rows["captured_at"]).total_seconds()
    assert delta == settings.annotation_ttl_minutes * 60


def test_the_posture_report_counts_a_kept_annotation(migrated):
    """`annotations_without_expiry` is the number keeping has to answer for."""
    from backend.extensions.compliance import posture
    aid = _seed("forklift")
    _patch(aid, keep=True, kept_reason="training")

    async def go(s):
        return await posture.derived_stills(s)
    out = _session(go)
    assert out["annotations"] == 1
    assert out["annotations_without_expiry"] == 1
