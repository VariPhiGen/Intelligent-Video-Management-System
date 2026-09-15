"""Does adding a camera leave a trace?

Until 2026-09-09 it did not, when added the way the product's own wizard adds
one. There are two creation paths and only one was audited:

  * POST /api/cameras                    -> cameras.py records `camera.created`
  * POST /api/discovery/devices/manual   -> discovery.py recorded NOTHING, and
    POST /api/discovery/devices/{id}/add    `grep audit discovery.py` was empty

The "Add cameras" wizard posts to the second pair, so in practice no camera
addition was ever audited. Found by clean-room test: after adding a camera and
building two evidence exports, the hash-chained log held seven rows — two failed
logins, a sign-in and four evidence rows — and nothing for the camera.

That is the wrong way round for a DPDP appliance. Registering a camera is the
act that STARTS collecting personal data; exporting it was already recorded
twice over.

Same shape as test_search_audit.py and test_sub_track_wiring.py: drive the real
code with every collaborator stubbed and assert only on what reached the audit
service. Both links are pinned separately, because either one alone still
produces a silent add:

  1. `_register` records the event                       (it never did)
  2. the endpoints hand `request` down to `_register`     (or the row lands
     with no actor, which is a trail that cannot answer "who")

Run (from services/camera-mgmt): python -m pytest tests -q
"""
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import settings  # noqa: E402
from backend.routers import discovery as disc  # noqa: E402


class _DB:
    """Enough AsyncSession for the register path; records nothing of its own."""
    def __init__(self): self.commits = 0
    async def commit(self): self.commits += 1
    async def rollback(self): return None
    async def refresh(self, obj): return None
    async def flush(self): return None
    def add(self, obj): return None
    async def execute(self, *a, **kw):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []),
                               scalar_one_or_none=lambda: None)


def _camera():
    return SimpleNamespace(
        id=uuid.uuid4(), ip="10.0.0.5", slug=None, name=None, rtsp_url=None,
        enabled=False, recording=True, stage="discovered", vendor=None,
        rtsp_port=554, rtsp_candidates=[], enc_username=None, enc_password=None,
        privacy_masks=[], camera_metadata={}, discovery_error=None,
        last_scanned_at=None,
    )


@pytest.fixture
def wired(monkeypatch):
    """Everything stubbed except the audit service, which is captured."""
    audits = []

    async def record(request, prin, action, *, target=None, detail=None, outcome="success"):
        audits.append({"action": action, "target": target, "detail": detail,
                       "principal": prin})

    async def principal(request):
        return SimpleNamespace(roles=["admin"], username="admin")

    async def ensure_unique(slug, db): return None

    monkeypatch.setattr(disc.audit_svc, "record", record)
    monkeypatch.setattr(disc, "get_principal", principal)
    monkeypatch.setattr(disc, "_ensure_unique_slug", ensure_unique)
    monkeypatch.setattr(disc, "validate_lawful_basis",
                        lambda b, p: (b or "public_safety", p or "test"))
    monkeypatch.setattr(disc, "generate_slug", lambda n: "gate-a1b2")
    # The relay/NVR/substream pushes have their own coverage; this is the trail.
    monkeypatch.setattr(settings, "nvr_sync_enabled", False)
    return SimpleNamespace(audits=audits, db=_DB(),
                           req=SimpleNamespace(headers={}, query_params={}))


# ── Link 1: _register records the event ──────────────────────────────────────

@pytest.mark.asyncio
async def test_registering_a_camera_is_audited(wired):
    """The whole finding in one assertion."""
    ok = await disc._register(
        _camera(), wired.db, request=wired.req, name="Gate",
        rtsp_url="rtsp://u:pw@10.0.0.5:554/main", enabled=False,
        lawful_basis="public_safety", purpose="perimeter",
    )
    assert ok is True
    created = [a for a in wired.audits if a["action"] == "camera.created"]
    assert len(created) == 1, "adding a camera must leave a trace"
    assert created[0]["target"] == "gate-a1b2"


@pytest.mark.asyncio
async def test_the_audited_actor_is_the_operator_not_nobody(wired):
    """`request` has to survive the hop, or the row cannot answer 'who'."""
    await disc._register(
        _camera(), wired.db, request=wired.req, name="Gate",
        rtsp_url="rtsp://u:pw@10.0.0.5:554/main", enabled=False,
        lawful_basis="public_safety", purpose="perimeter",
    )
    assert wired.audits[0]["principal"] is not None, (
        "an audit row with no principal records that a camera appeared, not "
        "that a person added one"
    )


@pytest.mark.asyncio
async def test_a_duplicate_rtsp_url_is_not_recorded_as_a_creation(wired, monkeypatch):
    """Guard the other way: a trail that logs adds which did not happen is as
    useless as one that misses adds which did. `_register` returns False when
    the URL already belongs to another camera."""
    from sqlalchemy.exc import IntegrityError

    class _FailOnceDB(_DB):
        async def commit(self):
            raise IntegrityError("stmt", {}, Exception("ix_cameras_rtsp_url"))

    db = _FailOnceDB()
    # The rollback path commits the error message on a second call.
    calls = {"n": 0}
    async def commit():
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError("stmt", {}, Exception("ix_cameras_rtsp_url"))
    db.commit = commit

    ok = await disc._register(
        _camera(), db, request=wired.req, name="Gate",
        rtsp_url="rtsp://u:pw@10.0.0.5:554/main", enabled=False,
        lawful_basis="public_safety", purpose="perimeter",
    )
    assert ok is False
    assert [a for a in wired.audits if a["action"] == "camera.created"] == []


# ── Link 2: the endpoints hand `request` down ────────────────────────────────

@pytest.mark.asyncio
async def test_the_manual_add_endpoint_passes_the_request_down(wired, monkeypatch):
    """The wizard's 'Manual IP / RTSP' option. Deleting `request=request` at the
    call site leaves _register auditing with principal None — a silent
    downgrade this catches and the endpoint's own response would not."""
    seen = {}

    async def fake_register(camera, db, **kw):
        seen.update(kw)
        return True

    monkeypatch.setattr(disc, "_register", fake_register)
    monkeypatch.setattr(disc, "_out", lambda c: c)
    monkeypatch.setattr(disc, "encrypt", lambda v: b"enc")

    body = disc.ManualAddBody(rtsp_url="rtsp://u:pw@10.0.0.5:554/main",
                              lawful_basis="public_safety", purpose="perimeter",
                              verify=False)
    await disc.add_manual(body, wired.req, wired.db)
    assert seen.get("request") is wired.req, (
        "add_manual must hand the request to _register or the audit row has no actor"
    )
