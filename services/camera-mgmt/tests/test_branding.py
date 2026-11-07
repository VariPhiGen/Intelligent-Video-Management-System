"""Branding (client logo + Video Wall title): admin-set, readable by every operator.

Endpoints are called directly with the database and the audit service stubbed (same shape as
test_search_audit.py). What matters: only PNG/JPEG/WebP logos are accepted (no SVG — it can carry
script), the size cap holds, a blank title means "default", and every change is audited.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.models import BRANDING_LOGO_MAX_BYTES  # noqa: E402
from backend.routers import branding as br  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


class FakeDB:
    def __init__(self):
        self.row = None
        self.commits = 0

    async def execute(self, _stmt):
        row = self.row
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: row))

    def add(self, row):
        self.row = row

    async def commit(self):
        self.commits += 1


class FakeUpload:
    def __init__(self, data):
        self.data = data

    async def read(self, n=-1):
        return self.data if n < 0 else self.data[:n]


@pytest.fixture
def env(monkeypatch):
    audits = []

    async def record(request, principal, action, target=None, detail=None):
        audits.append((action, detail))
    monkeypatch.setattr(br.audit_svc, "record", record)
    return FakeDB(), SimpleNamespace(subject="admin"), audits


def run(coro):
    return asyncio.run(coro)


def test_defaults_before_anything_is_set(env):
    db, _, _ = env
    meta = run(br.get_branding(db=db))
    assert meta.wall_title is None and meta.has_logo is False and meta.logo_version is None


def test_title_set_and_blank_resets_to_default(env):
    db, admin, audits = env
    assert run(br.set_branding(br.BrandingBody(wall_title="  Plant 3 — Control Room "), None, admin, db)).wall_title \
        == "Plant 3 — Control Room"
    assert run(br.set_branding(br.BrandingBody(wall_title="   "), None, admin, db)).wall_title is None
    assert [a for a, _ in audits] == ["branding.updated", "branding.updated"]


def test_png_logo_round_trip_with_etag(env):
    db, admin, audits = env
    meta = run(br.upload_logo(None, FakeUpload(PNG), admin, db))
    assert meta.has_logo and meta.logo_version
    req = SimpleNamespace(headers={})
    resp = run(br.get_logo(req, db))
    assert resp.body == PNG and resp.media_type == "image/png" and "etag" in {k.lower() for k in resp.headers}
    assert resp.headers.get("x-content-type-options") == "nosniff"
    again = run(br.get_logo(SimpleNamespace(headers={"if-none-match": resp.headers["etag"]}), db))
    assert again.status_code == 304
    assert ("branding.logo_set", {"content_type": "image/png", "bytes": len(PNG), "theme": "light"}) in audits


def test_svg_and_unknown_types_are_rejected(env):
    db, admin, _ = env
    for data in (SVG, b"GIF89a....", b"not an image"):
        with pytest.raises(HTTPException) as e:
            run(br.upload_logo(None, FakeUpload(data), admin, db))
        assert e.value.status_code == 422
    assert db.row is None


def test_oversized_logo_is_rejected(env):
    db, admin, _ = env
    with pytest.raises(HTTPException) as e:
        run(br.upload_logo(None, FakeUpload(PNG + b"\x00" * BRANDING_LOGO_MAX_BYTES), admin, db))
    assert e.value.status_code == 422 and "MB" in e.value.detail


def test_delete_logo(env):
    db, admin, audits = env
    run(br.upload_logo(None, FakeUpload(PNG), admin, db))
    meta = run(br.delete_logo(None, admin, db))
    assert meta.has_logo is False
    with pytest.raises(HTTPException) as e:
        run(br.get_logo(SimpleNamespace(headers={}), db))
    assert e.value.status_code == 404
    assert audits[-1][0] == "branding.logo_removed"


def test_changes_are_admin_only_and_router_is_mounted():
    src = (Path(__file__).resolve().parents[1] / "backend" / "routers" / "branding.py").read_text()
    assert src.count("Depends(_ADMIN)") == 3          # PUT title, POST logo, DELETE logo
    main = (Path(__file__).resolve().parents[1] / "backend" / "main.py").read_text()
    assert "branding_router, prefix=\"/api\", dependencies=[Depends(require_authenticated)]" in main


def test_dark_logo_is_separate_from_the_light_one(env):
    db, admin, _ = env
    run(br.upload_logo(None, FakeUpload(PNG), admin, db, theme="dark"))
    meta = run(br.get_branding(db=db))
    assert meta.has_logo_dark and not meta.has_logo and meta.logo_dark_version
    assert run(br.get_logo(SimpleNamespace(headers={}), db, theme="dark")).body == PNG
    with pytest.raises(HTTPException):
        run(br.get_logo(SimpleNamespace(headers={}), db))          # light still unset
    assert run(br.delete_logo(None, admin, db, theme="dark")).has_logo_dark is False


def test_wall_defaults_saved_camelcase_and_title_left_alone(env):
    db, admin, audits = env
    run(br.set_branding(br.BrandingBody(wall_title="Control Room"), None, admin, db))
    body = br.BrandingBody.model_validate({"wall_defaults": {
        "followAll": False, "cameraIds": ["a", "b"], "topPad": 80, "bgDark": "#0b1f3a", "titleColorLight": "#111111"}})
    meta = run(br.set_branding(body, None, admin, db))
    assert meta.wall_title == "Control Room"                       # PATCH-like: title untouched
    d = meta.wall_defaults
    assert d["followAll"] is False and d["cameraIds"] == ["a", "b"] and d["topPad"] == 80
    assert d["bgDark"] == "#0b1f3a" and d["hideOffline"] is True and d["logoH"] == 36
    assert audits[-1][1]["wall_defaults"]["cameras"] == 2
    # saving the title later does not wipe the defaults; an explicit null resets them
    assert run(br.set_branding(br.BrandingBody(wall_title="X"), None, admin, db)).wall_defaults["topPad"] == 80
    reset = br.BrandingBody.model_validate({"wall_defaults": None})
    assert run(br.set_branding(reset, None, admin, db)).wall_defaults is None


def test_wall_defaults_reject_unsafe_colours_and_out_of_range_sizes():
    from pydantic import ValidationError
    for bad in ({"bgLight": "red;background:url(x)"}, {"titleColorDark": "#12345"}, {"topPad": 999}, {"titlePx": 4}):
        with pytest.raises(ValidationError):
            br.WallDefaults.model_validate(bad)
