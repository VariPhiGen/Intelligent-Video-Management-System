"""Camera inventory Excel export: name + camera ID first, every field an admin needs, and NEVER a
credential — not the encrypted username/password, not a password embedded in an RTSP URL.

Two layers, deliberately:
  • the builder, called directly — what lands in which cell, and what must never land in any cell;
  • the route, over real ASGI with a real RS256 token — that /export.xlsx is reachable at all (it
    sits in front of /{camera_id}, which would otherwise swallow it as a malformed UUID) and that
    an operator with 'export_reports' still cannot pull the fleet's reachability.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
import io
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import load_workbook  # noqa: E402

from backend.services import camera_export as ce  # noqa: E402

URL = "/api/cameras/export.xlsx"
CID = uuid.UUID("3f1c2b7e-0000-4000-8000-000000000001")


def _cam(**kw):
    base = dict(
        id=CID, name="Weld Shop West Side View-2", slug="cpplus-cp-unc-tc41l5c-vmd-lq",
        enabled=True, recording=True, ip="192.168.0.37", mac="aa:bb:cc:dd:ee:ff", vendor="CPPLUS",
        model="CP-UNC-TC41L5C-VMD-LQ", firmware="2.860.00AT002.0.R", onvif_port=80,
        rtsp_port=554, rtsp_url="rtsp://admin:S3cret%40pw@192.168.0.37:554/cam/realmonitor?channel=1",
        sub_track={"url_raw": "rtsp://192.168.0.37/sub"}, health_status="unknown",
        recording_schedule=None, retention_minutes=43200, motion_detection=False,
        motion_sensitivity=None, search_indexing=False, search_domains=["person", "vehicles"],
        ptz_capabilities=None,
        last_seen_at=datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc),
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc), updated_at=None, discovery_error=None,
        enc_username="gAAAA-ENC-USER", enc_password="gAAAA-ENC-PASS",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _row(cam=None, **kw):
    opts = dict(zone="Kec", streaming=True, uptime_pct=98.5,
                relay_url="rtsp://127.0.0.1:8654/cpplus-cp-unc-tc41l5c-vmd-lq")
    opts.update(kw)
    return ce.camera_row(cam if cam is not None else _cam(), **opts)


def _book(rows, **kw):
    data = ce.build_workbook(rows, generated_by="admin", **kw)
    return load_workbook(io.BytesIO(data))


def _rec(ws):
    """Header → value for the single data row."""
    return dict(zip([c.value for c in ws[1]], [c.value for c in ws[2]]))


def test_strip_credentials():
    assert ce.strip_credentials("rtsp://admin:pw@10.0.0.5:554/x?y=1") == "rtsp://10.0.0.5:554/x?y=1"
    assert ce.strip_credentials("rtsp://10.0.0.5/x") == "rtsp://10.0.0.5/x"
    assert ce.strip_credentials(None) == "" and ce.strip_credentials("") == ""


def test_name_and_id_lead_and_no_credentials_anywhere():
    wb = _book([_row()])
    ws = wb["Cameras"]
    header = [c.value for c in ws[1]]
    assert header[:2] == ["Name", "Camera ID"]
    values = [c.value for c in ws[2]]
    assert values[0] == "Weld Shop West Side View-2" and values[1] == str(CID)
    rec = dict(zip(header, values))
    assert rec["Status"] == "Streaming" and rec["Zone"] == "Kec" and rec["Uptime 7d %"] == 98.5
    assert rec["Camera RTSP URL (no credentials)"] == "rtsp://192.168.0.37:554/cam/realmonitor?channel=1"
    assert rec["Sub-stream"] == "Yes" and rec["MAC address"] == "aa:bb:cc:dd:ee:ff"
    # nothing secret in ANY cell of ANY sheet
    dump = " ".join(str(c.value) for s in wb.worksheets for r in s.iter_rows() for c in r if c.value is not None)
    for secret in ("S3cret", "admin:", "gAAAA-ENC-USER", "gAAAA-ENC-PASS"):
        assert secret not in dump
    assert ws.freeze_panes == "B2" and ws.auto_filter.ref.startswith("A1:")
    assert isinstance(rec[next(h for h in header if h.startswith("Last seen"))], datetime)


def test_status_reflects_the_relay_and_summary_counts():
    rows = [
        _row(streaming=True, uptime_pct=None),
        _row(_cam(id=uuid.uuid4(), name="B"), streaming=False, uptime_pct=None),
        _row(_cam(id=uuid.uuid4(), name="C", enabled=False), streaming=False, uptime_pct=None),
        _row(_cam(id=uuid.uuid4(), name="D"), zone="Unzoned", streaming=None, uptime_pct=None),
    ]
    assert [r[ce.STATUS_COL - 1] for r in rows] == ["Streaming", "Not streaming", "Disabled", "Unknown"]
    s = _book(rows, selection=True)["Summary"]
    kv = {r[0].value: r[1].value for r in s.iter_rows() if r[0].value}
    assert kv["Cameras"] == 4 and kv["Streaming"] == 1 and kv["Not streaming"] == 1
    assert kv["Scope"] == "Selected cameras" and kv["Kec"] == 3 and kv["Unzoned"] == 1
    # The retention breakdown is a disk-planning answer, so it has to count every camera.
    assert kv["30 days"] == 4


def test_operational_posture_columns():
    """Recording schedule, retention, motion, Smart Search and PTZ are on the row, not the page —
    the whole reason the export is server-side."""
    rec = _rec(_book([_row()])["Cameras"])
    # NULL schedule is 24/7, not "unknown"; NULL retention is the appliance default, spelled as
    # retention_units spells it everywhere else.
    assert rec["Recording schedule"] == "24/7" and rec["Retention"] == "30 days"
    assert rec["Motion detection"] == "No" and rec["Smart Search"] == "Off"
    assert rec["PTZ"] == "Unknown"  # never probed is its own answer
    assert not rec["ONVIF over HTTPS"]  # NULL: never needed it — a blank cell, as on field
    assert _rec(_book([_row(_cam(onvif_use_https=True))])["Cameras"])["ONVIF over HTTPS"] == "Yes"

    cam = _cam(
        recording_schedule={"mode": "weekly", "rules": [{"days": [1], "start": "08:00", "end": "18:00"}]},
        retention_minutes=None, motion_detection=True, motion_sensitivity="high",
        search_indexing=True, search_domains=["person"],
        ptz_capabilities={"supported": True, "profile_token": "p1"},
    )
    rec = _rec(_book([_row(cam)])["Cameras"])
    assert rec["Recording schedule"] == "Weekly (1 rule)"
    assert rec["Retention"] == "appliance default"
    assert rec["Motion detection"] == "Yes (high)" and rec["Smart Search"] == "person"
    assert rec["PTZ"] == "Yes"
    assert _rec(_book([_row(_cam(ptz_capabilities={"supported": False, "reason": "fixed"}))])
               ["Cameras"])["PTZ"] == "No"


def test_a_camera_name_is_never_a_formula():
    """A camera named '=cmd|...' must arrive as TEXT. openpyxl types a leading '=' as a formula, so
    without the fix the inventory executes in whoever opens it."""
    name = '=cmd|\' /c calc\'!A1'
    wb = _book([_row(_cam(name=name, discovery_error="-2 ONVIF timeout", model="@AXIS"))])
    ws = wb["Cameras"]
    for cell in ws[2]:
        assert cell.data_type != "f"
    rec = _rec(ws)
    assert rec["Name"] == name and rec["Discovery error"] == "-2 ONVIF timeout"
    # ...including the Summary tables, whose keys are camera data too.
    assert any(r[0].value == "@AXIS" for r in wb["Summary"].iter_rows() if r[0].value)


def test_an_empty_fleet_still_builds():
    """Export with nothing selected-and-registered: a 1-row autofilter, not a crash."""
    wb = _book([])
    assert wb["Cameras"].max_row == 1 and wb["Cameras"].auto_filter.ref == "A1:AC1"
    kv = {r[0].value: r[1].value for r in wb["Summary"].iter_rows() if r[0].value}
    assert kv["Cameras"] == 0 and kv["Scope"] == "All cameras"


# ── The route ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_gets_a_workbook_and_the_route_sits_before_the_uuid_route(client, mint):
    resp = await client.get(URL, headers=mint(roles=["admin"]))
    # 422 here would mean FastAPI matched /{camera_id} and tried to parse "export.xlsx" as a UUID.
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith(ce.XLSX_MEDIA_TYPE)
    assert 'filename="cameras_' in resp.headers["content-disposition"]
    assert resp.headers["cache-control"] == "no-store"
    wb = load_workbook(io.BytesIO(resp.content))
    assert wb.sheetnames == ["Cameras", "Summary"]
    assert [c.value for c in wb["Cameras"][1]][:2] == ["Name", "Camera ID"]


@pytest.mark.asyncio
@pytest.mark.parametrize("roles", [["operator"], ["supervisor"], ["viewer"], []])
async def test_the_fleet_inventory_is_admin_only(client, mint, roles):
    """'export_reports' (operator and supervisor have it by default) buys the uptime workbook, not
    every camera's IP, MAC, ports and stream URLs."""
    resp = await client.get(URL, headers=mint(roles=roles))
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_unauthenticated_is_refused(client):
    assert (await client.get(URL)).status_code in (401, 403)


@pytest.mark.asyncio
async def test_a_bad_ids_list_is_a_422_not_a_500(client, mint):
    resp = await client.get(f"{URL}?ids=not-a-uuid", headers=mint(roles=["admin"]))
    assert resp.status_code == 422, resp.text
