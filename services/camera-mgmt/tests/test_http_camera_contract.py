"""The camera and device API, asserted as a contract.

The frontend is 23,220 lines built against these endpoints and until now nothing
asserted what they return — not a status code, not a body shape, not the shape
of an error. The handlers' logic is well covered by the direct-call suites; what
was missing is everything FastAPI does around them: parsing a path parameter,
validating a body against CameraCreate, choosing a status code, serialising a
response model, and turning an exception into a body the SPA can read.

Representative rather than exhaustive, per the plan: the camera lifecycle, the
DPDP purpose-binding that gates registration, and one device surface
(discovery). Endpoints whose only interesting property is "is it guarded" live
in test_http_auth_contract.py instead of being repeated here.

Requests are made as an admin unless a test is about who may call something —
authorisation has its own file, and mixing the two makes a failure ambiguous.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


# A body that genuinely registers. The lawful basis must come from
# models.LAWFUL_BASES verbatim — an invented one is refused by the DPDP gate
# before any other validation runs, which silently turns every test below into
# a test of that gate instead of the field it names.
VALID_CAMERA = {
    "name": "Front Gate",
    "rtsp_url": "rtsp://user:pass@10.0.0.5:554/Streaming/Channels/101",
    "lawful_basis": "Public safety / State function",
    "purpose": "Perimeter monitoring at the main entrance",
}


@pytest.fixture
def admin(mint):
    return mint(["admin"])


# ── Request validation ─────────────────────────────────────────────────────

@pytest.mark.parametrize("missing", ["name", "rtsp_url"])
async def test_creating_a_camera_without_a_required_field_is_422(client, admin, missing):
    body = {k: v for k, v in VALID_CAMERA.items() if k != missing}
    r = await client.post("/api/cameras", headers=admin, json=body)
    assert r.status_code == 422
    assert any(missing in str(e.get("loc", "")) for e in r.json()["detail"]), r.text


@pytest.mark.parametrize("url", [
    "http://10.0.0.5/stream",       # wrong scheme entirely
    "https://10.0.0.5/stream",
    "10.0.0.5:554/Streaming",       # no scheme
    "",
    "rtsp://",                       # passes the scheme check, fails min_length
])
async def test_a_camera_url_that_is_not_rtsp_is_refused(client, admin, url):
    """`_rtsp_scheme` is the only thing standing between the relay and a URL
    MediaMTX cannot pull. A camera created with one looks registered and never
    produces a frame."""
    r = await client.post("/api/cameras", headers=admin,
                          json={**VALID_CAMERA, "rtsp_url": url})
    assert r.status_code == 422, f"{url!r} was accepted"


async def test_an_empty_camera_name_is_refused(client, admin):
    r = await client.post("/api/cameras", headers=admin,
                          json={**VALID_CAMERA, "name": ""})
    assert r.status_code == 422


async def test_an_over_long_camera_name_is_refused(client, admin):
    r = await client.post("/api/cameras", headers=admin,
                          json={**VALID_CAMERA, "name": "x" * 256})
    assert r.status_code == 422


async def test_a_wrongly_typed_field_is_422_not_500(client, admin):
    """`enabled` is a bool. A string there must be a validation error, not an
    exception surfacing as a server fault."""
    r = await client.post("/api/cameras", headers=admin,
                          json={**VALID_CAMERA, "enabled": "yes-please"})
    assert r.status_code == 422


async def test_a_malformed_body_is_422_not_500(client, admin):
    r = await client.post(
        "/api/cameras", headers={**admin, "Content-Type": "application/json"},
        content=b"{not json",
    )
    assert r.status_code == 422


async def test_a_camera_id_that_is_not_a_uuid_is_422(client, admin):
    """The path parameter is typed `uuid.UUID`; a junk id must be rejected by
    the router rather than reaching a query."""
    r = await client.get("/api/cameras/not-a-uuid", headers=admin)
    assert r.status_code == 422


async def test_a_slug_ending_in_the_sub_suffix_is_refused(client, admin):
    """`_sub` is reserved for a camera's low-resolution track: a camera slugged
    into that namespace would share a recording name with another camera's sub
    and the two would overwrite each other's footage.

    Asserted on the refusal, not on which check produced it. Today the
    underscore is rejected by the slug character pattern and the explicit
    reserved-suffix guard behind it never runs — see
    test_tracks_wiring.py::test_the_reservation_survives_a_pattern_that_allows
    _the_suffix, which drives that guard directly.
    """
    r = await client.post("/api/cameras", headers=admin,
                          json={**VALID_CAMERA, "slug": "gate_sub"})
    assert r.status_code == 422


# ── DPDP purpose-binding, which gates registration ─────────────────────────

async def test_registering_without_a_lawful_basis_is_refused(client, admin):
    """A registered camera must carry a lawful basis and purpose. This is a
    compliance gate, not a nicety — it is why the field exists."""
    body = {k: v for k, v in VALID_CAMERA.items() if k != "lawful_basis"}
    r = await client.post("/api/cameras", headers=admin, json=body)
    assert r.status_code in (400, 422), r.text


async def test_an_invented_lawful_basis_is_refused(client, admin):
    """The set is closed (DPDP s.7 legitimate uses). A free-text basis would
    make the register unauditable."""
    r = await client.post("/api/cameras", headers=admin,
                          json={**VALID_CAMERA, "lawful_basis": "because-i-said-so"})
    assert r.status_code in (400, 422), r.text


# ── The success path, and the shape it returns ─────────────────────────────

async def test_registering_a_camera_returns_201(client, admin):
    """The positive case. Without it every 422 above could be produced by the
    endpoint being broken outright, and the suite could not tell the
    difference."""
    r = await client.post("/api/cameras", headers=admin, json=VALID_CAMERA)
    assert r.status_code == 201, r.text


async def test_a_created_camera_comes_back_as_a_camera_response(client, admin):
    """The body the SPA renders a camera row from, asserted on a real response
    rather than on the schema."""
    r = await client.post("/api/cameras", headers=admin, json=VALID_CAMERA)
    assert r.status_code == 201
    body = r.json()
    for field in ("id", "name", "slug", "rtsp_url", "local_rtsp_url",
                  "enabled", "recording", "health_status", "created_at"):
        assert field in body, f"CameraResponse is missing {field}"
    assert body["name"] == VALID_CAMERA["name"]
    assert body["lawful_basis"] == VALID_CAMERA["lawful_basis"]


async def test_a_new_camera_gets_a_generated_slug(client, admin):
    """`slug` is optional on create and is derived from the name with a random
    4-character suffix. It is the join key for detections, events, bookmarks
    and audit, so it must never come back empty."""
    r = await client.post("/api/cameras", headers=admin, json=VALID_CAMERA)
    slug = r.json()["slug"]
    assert slug.startswith("front-gate-")
    assert not slug.endswith("_sub")


async def test_an_unattested_camera_reports_unknown_not_a_plausible_value(client, admin):
    """The honest-UI rule, at the API boundary: STQC posture is optional at
    registration, and a camera that asserted nothing must say "unknown" rather
    than defaulting to a status it has not earned."""
    r = await client.post("/api/cameras", headers=admin, json=VALID_CAMERA)
    body = r.json()
    assert body["stqc_status"] == "unknown"
    assert body["stqc_certificate_no"] is None
    assert body["stqc_verified_at"] is None


# ── Status codes and response shape ────────────────────────────────────────

async def test_listing_cameras_returns_a_json_array(client, admin):
    r = await client.get("/api/cameras", headers=admin)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert isinstance(r.json(), list)


async def test_a_missing_camera_is_404_with_a_readable_detail(client, admin, camera_id):
    r = await client.get(f"/api/cameras/{camera_id}", headers=admin)
    assert r.status_code == 404
    assert r.json()["detail"] == "Camera not found"


async def test_deleting_a_missing_camera_is_404(client, admin, camera_id):
    r = await client.delete(f"/api/cameras/{camera_id}", headers=admin)
    assert r.status_code == 404


async def test_an_error_body_is_always_a_detail_object(client, admin, camera_id):
    """The SPA reads `detail` on every failure. A handler that returns a bare
    string or a different key renders as an empty error toast."""
    for path in (f"/api/cameras/{camera_id}", "/api/cameras/not-a-uuid"):
        r = await client.get(path, headers=admin)
        assert r.status_code >= 400
        body = r.json()
        assert isinstance(body, dict) and "detail" in body, (path, body)


async def test_an_unsupported_method_is_405(client, admin):
    r = await client.patch("/api/cameras", headers=admin, json={})
    assert r.status_code == 405


async def test_an_unknown_api_path_is_404(client, admin):
    r = await client.get("/api/cameras/uptime/nonexistent-report", headers=admin)
    assert r.status_code in (404, 422)


# ── The response model actually shapes the response ────────────────────────

async def test_the_camera_list_is_declared_with_a_response_model(client):
    """A route that loses its response_model starts returning whatever the ORM
    object happens to carry, including columns added later.

    Asserted through the generated OpenAPI document rather than by walking
    `app.routes`. The route objects are a FastAPI internal and their shape
    changes between versions — 0.115 flattens included routers into
    `app.routes` while 0.138 holds them lazily behind `_IncludedRouter`, so a
    walk that works on the pinned version silently finds nothing on a newer
    one and the test passes by vacuum or fails for no reason of the author's.
    The OpenAPI document is the public contract and is stable across both.
    """
    r = await client.get("/openapi.json")
    schema = r.json()["paths"]["/api/cameras"]["get"]["responses"]["200"]
    body = schema["content"]["application/json"]["schema"]
    assert body["type"] == "array"
    assert body["items"]["$ref"].endswith("/CameraResponse")


async def test_openapi_describes_the_camera_api(client):
    """The generated schema is what the frontend and any integrator read. If
    the app cannot produce it, the routes are malformed."""
    r = await client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert "/api/cameras" in paths
    assert "post" in paths["/api/cameras"]
    assert "get" in paths["/api/cameras"]


async def test_the_camera_response_schema_still_carries_its_key_fields(client):
    """These are the fields the SPA renders a camera row from. Removing one is
    a breaking change and should have to be done deliberately."""
    r = await client.get("/openapi.json")
    props = r.json()["components"]["schemas"]["CameraResponse"]["properties"]
    for field in ("id", "name", "slug", "rtsp_url", "enabled",
                  "recording", "health_status"):
        assert field in props, f"CameraResponse lost {field}"


# ── A device surface: discovery ────────────────────────────────────────────

async def test_listing_discovered_devices_needs_camera_manage(client, mint):
    """The whole discovery mount is gated on the capability, not a role —
    onboarding a camera is delegable."""
    r = await client.get("/api/discovery/devices", headers=mint(["viewer"]))
    assert r.status_code == 403


async def test_a_supervisor_may_list_discovered_devices(client, mint):
    r = await client.get("/api/discovery/devices", headers=mint(["supervisor"]))
    assert r.status_code == 200


async def test_adding_a_manual_device_validates_its_body(client, admin):
    r = await client.post("/api/discovery/devices/manual", headers=admin, json={})
    assert r.status_code == 422


async def test_a_discovery_device_id_that_is_not_a_uuid_is_422(client, admin):
    r = await client.delete("/api/discovery/devices/nope", headers=admin)
    assert r.status_code == 422
