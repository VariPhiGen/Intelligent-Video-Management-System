"""`ready_since` means the same thing on the list and on one camera.

THE FIELD IS DOCUMENTED ON THE MODEL, not on one endpoint: CameraResponse calls
it "when the relay stream last became ready (MediaMTX readyTime) — the true 'up
since' for uptime display", and says it is None only when the stream is down or
MediaMTX was unreachable. Nothing in that description is list-shaped.

It was list-shaped anyway. `list_cameras` enriched every row from one relay
call; `get_camera` returned `_build_response(camera)` untouched, so
`GET /api/cameras/{id}` reported a camera that had been streaming for a week as
having no ready time at all. Latent rather than visible — the SPA's
`useCameras()` drives the health tab off the LIST — which is precisely why it
could sit wrong indefinitely: no screen was wired to the endpoint that lied.

These tests are about the two surfaces AGREEING, not about either one's
implementation. They stub the relay and compare what the two handlers answer for
the same camera and the same relay state, so a future change that enriches the
detail endpoint some other way still passes, and one that reintroduces the
divergence still fails.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.models import Camera, CameraStage  # noqa: E402
from backend.routers import cameras as cam_router  # noqa: E402
from backend.services import relay  # noqa: E402


SLUG = "gate-a1b2"
CAM_ID = uuid.uuid4()
# What MediaMTX actually answers: nanosecond precision, which is why the
# normalisation truncates rather than parsing.
READY_TIME = "2026-09-11T04:15:09.123456789Z"
READY_SINCE = "2026-09-11T04:15:09Z"
NOW = datetime(2026, 9, 11, 4, 0, 0, tzinfo=timezone.utc)


def _camera():
    """A registered Camera row, unpersisted. A real ORM object rather than a
    namespace, so `CameraResponse.from_orm` reads the columns it really reads."""
    return Camera(
        id=CAM_ID, name="Front Gate", slug=SLUG,
        rtsp_url="rtsp://user:pw@10.0.0.5:554/main",
        enabled=True, recording=True, health_status="connected",
        stage=CameraStage.REGISTERED.value,
        # Columns whose database defaults are applied on flush, not on
        # construction. Set explicitly because this row is never persisted and
        # CameraResponse requires them — unrelated to what is under test.
        created_at=NOW, updated_at=NOW, camera_metadata={},
        motion_detection=False, search_indexing=False, notice_posted=False,
    )


class _DB:
    """Returns one camera for both the list query and the by-id lookup."""
    def __init__(self, camera):
        self._camera = camera

    async def execute(self, *a, **kw):
        cam = self._camera
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [cam]),
            scalar_one_or_none=lambda: cam,
        )


def _stub_relay(monkeypatch, *, path: dict | None, explode: bool = False):
    """Point both enrichment routes at the same fake relay state.

    `path` is one MediaMTX path entry as the API returns it, or None for a
    camera the relay does not hold.
    """
    async def list_active_paths():
        if explode:
            raise RuntimeError("MediaMTX is unreachable")
        return [path] if path else []

    async def get_path_status(slug):
        if explode:
            raise RuntimeError("MediaMTX is unreachable")
        if not path or path.get("name") != slug:
            return {"connected": False, "registered": False, "source_type": None,
                    "tracks": [], "readers": [], "ready_time": None}
        return {"connected": bool(path.get("ready")), "registered": True,
                "source_type": None, "tracks": [], "readers": [],
                # The real implementation's own normalisation, not a second
                # copy of the rule — that duplication is what caused this bug.
                "ready_time": relay.ready_since_of(path)}

    monkeypatch.setattr(cam_router.relay, "list_active_paths", list_active_paths)
    monkeypatch.setattr(cam_router.relay, "get_path_status", get_path_status)


async def _both(monkeypatch, *, path, explode=False):
    """(list answer, detail answer) for the same camera and relay state."""
    cam = _camera()
    db = _DB(cam)
    _stub_relay(monkeypatch, path=path, explode=explode)
    listed = await cam_router.list_cameras(enabled=None, skip=0, limit=100, db=db)
    detail = await cam_router.get_camera(CAM_ID, db=db)
    return listed[0], detail


READY_PATH = {"name": SLUG, "ready": True, "readyTime": READY_TIME}


# ── The regression ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_one_camera_reports_the_ready_time_it_is_streaming_since(monkeypatch):
    """THE BUG. This endpoint answered None for every streaming camera."""
    _, detail = await _both(monkeypatch, path=READY_PATH)
    assert detail.ready_since == READY_SINCE


@pytest.mark.asyncio
async def test_the_list_and_the_detail_agree(monkeypatch):
    """THE INVARIANT, and the reason this file exists. One field, one meaning,
    whichever endpoint is asked."""
    listed, detail = await _both(monkeypatch, path=READY_PATH)
    assert listed.ready_since == detail.ready_since == READY_SINCE


@pytest.mark.asyncio
async def test_they_agree_that_a_down_stream_has_no_ready_time(monkeypatch):
    """A path that has gone down KEEPS its last readyTime, so `ready` has to be
    checked too — otherwise both endpoints would report an "up since" for a
    camera that is offline, which is a lie with a plausible value in it."""
    down = {"name": SLUG, "ready": False, "readyTime": READY_TIME}
    listed, detail = await _both(monkeypatch, path=down)
    assert listed.ready_since is None
    assert detail.ready_since is None


@pytest.mark.asyncio
async def test_they_agree_when_the_relay_does_not_hold_the_camera(monkeypatch):
    listed, detail = await _both(monkeypatch, path=None)
    assert listed.ready_since is None
    assert detail.ready_since is None


# ── The enrichment must not make either endpoint fragile ───────────────────

@pytest.mark.asyncio
async def test_neither_endpoint_fails_when_mediamtx_is_unreachable(monkeypatch):
    """Uptime is not worth a 5xx, and this is a real risk of the fix rather than
    a hypothetical: `get_camera` answered without touching the relay at all
    until now, so a relay outage must not turn every camera detail page into an
    error. Both enrichments are best-effort and the field simply goes None."""
    listed, detail = await _both(monkeypatch, path=None, explode=True)
    assert listed.ready_since is None
    assert detail.ready_since is None
    assert listed.slug == detail.slug == SLUG


# ── The shared definition itself ───────────────────────────────────────────

def test_nanosecond_precision_is_truncated_to_seconds():
    """`readyTime` carries nanoseconds that not every Date parser accepts; the
    SPA does `new Date(camera.ready_since)`."""
    assert relay.ready_since_of(READY_PATH) == READY_SINCE


def test_a_path_with_no_ready_time_is_not_invented():
    assert relay.ready_since_of({"name": SLUG, "ready": True}) is None
    assert relay.ready_since_of({}) is None
