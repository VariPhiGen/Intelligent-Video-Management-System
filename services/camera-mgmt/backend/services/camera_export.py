"""Camera inventory → Excel workbook (GET /api/cameras/export.xlsx).

Why server-side (the inventory already had a client-side CSV of the ticked rows):
  • the camera ID — the UUID integrations, the API and support tickets key on — was never in it;
  • most of what an admin needs (MAC, vendor, ONVIF/RTSP ports, retention, schedule, sub-stream,
    discovery errors) is not on the inventory page, so a browser export can only ever be as
    complete as the table;
  • one builder means every export of the fleet says the same thing, whichever screen it came from.

What is deliberately NOT in the file: camera usernames/passwords (stored encrypted — the whole point
of DISCOVERY_SECRET_KEY) and any credentials embedded in an RTSP URL. Inventories get emailed to
vendors and facilities; a spreadsheet must never be the copy that leaks a camera's admin password.

"Status" is what the relay says NOW (MediaMTX path ready = streaming), not the health_status column:
found live on a site where every row read "Unknown" while 16/18 were streaming — health_status lags
on installs where the health worker never ran, the relay is ground truth for "can I watch it".

This is the OPERATIONAL inventory. The DPDP posture of the same fleet (lawful basis, purpose, notice,
STQC, retention justification) is a separate report with its own attestation —
extensions/compliance/dpdpa_report.py — and is not duplicated here.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from ..urlutil import split_credentials
from .retention_units import format_retention

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# (header, width) — the order an admin reads a row in: who it is, where it is, what it records,
# how to reach it, what it can do, then its history.
COLUMNS: list[tuple[str, int]] = [
    ("Name", 30), ("Camera ID", 38), ("Short name (slug)", 30), ("Zone", 16),
    ("Status", 14), ("Uptime 7d %", 12), ("Enabled", 9), ("Recording", 10),
    ("Recording schedule", 20), ("Retention", 18),
    ("IP address", 16), ("MAC address", 19), ("Vendor", 14), ("Model", 24), ("Firmware", 22),
    ("ONVIF port", 11), ("ONVIF over HTTPS", 16), ("RTSP port", 10),
    ("Camera RTSP URL (no credentials)", 48), ("Relay RTSP URL", 42), ("Sub-stream", 11),
    ("Motion detection", 16), ("Smart Search", 22), ("PTZ", 10),
    ("Health (last recorded)", 20), ("Last seen", 20), ("Added", 20), ("Updated", 20),
    ("Discovery error", 40),
]
_H = [h for h, _ in COLUMNS]
STATUS_COL = _H.index("Status") + 1
_UPTIME_COL = _H.index("Uptime 7d %") + 1
_DT_COLS = [i + 1 for i, h in enumerate(_H) if h in ("Last seen", "Added", "Updated")]
_LOCAL_DT_HEADERS = ("Last seen", "Added", "Updated")

# openpyxl types a string starting with '=' as a FORMULA, so a camera named
# "=cmd|'/c calc'!A1" (or a discovery error that begins with one) would be written as <f> and
# evaluated when the sheet is opened. Every text cell that leads with one of these is forced
# back to a literal string — an inventory is data, and must never execute in the reader's Excel.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _append(ws, values: Iterable[Any]) -> None:
    """ws.append + de-formula every leading-'='/'+'/'-'/'@' text cell (see _FORMULA_LEAD)."""
    ws.append(list(values))
    for cell in ws[ws.max_row]:
        if isinstance(cell.value, str) and cell.value[:1] in _FORMULA_LEAD:
            cell.data_type = "s"


def strip_credentials(uri: Optional[str]) -> str:
    """rtsp://admin:pw@10.0.0.5:554/x → rtsp://10.0.0.5:554/x. Unparseable input → '' (never echo it:
    a malformed URL is exactly where a raw password would otherwise slip through)."""
    if not uri:
        return ""
    try:
        clean, _, _ = split_credentials(uri)
        # split_credentials rebuilds the netloc from hostname+port; belt-and-braces in case a
        # pathological URL still carries userinfo, drop it rather than export it.
        return "" if "@" in urlparse(clean).netloc else clean
    except Exception:
        return ""


def _excel_dt(dt: Optional[datetime]) -> Optional[datetime]:
    """openpyxl rejects tz-aware datetimes; write server-local wall time (the zone is in the header)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().replace(tzinfo=None)


def _yn(v: Any) -> str:
    return "" if v is None else ("Yes" if v else "No")


def _schedule_label(sched: Optional[dict]) -> str:
    """NULL recording_schedule means record whenever recording is on — say so, don't leave it blank
    (a blank cell in a 'schedule' column reads as "unknown", and 24/7 is the opposite of unknown)."""
    if not sched or not sched.get("rules"):
        return "24/7"
    mode = str(sched.get("mode") or "weekly").capitalize()
    n = len(sched.get("rules") or [])
    return f"{mode} ({n} rule{'' if n == 1 else 's'})"


def _motion_label(cam: Any) -> str:
    if not getattr(cam, "motion_detection", False):
        return "No"
    sens = getattr(cam, "motion_sensitivity", None)
    return f"Yes ({sens})" if sens else "Yes"


def _search_label(cam: Any) -> str:
    """Smart Search is opt-IN per camera and per domain; "Off" and "person" are different postures
    and the sheet has to be able to tell a site which of its cameras the AI ever looked at."""
    if not getattr(cam, "search_indexing", False):
        return "Off"
    domains = [str(d) for d in (getattr(cam, "search_domains", None) or []) if d]
    return ", ".join(domains) if domains else "On"


def _ptz_label(cam: Any) -> str:
    """Three states, same as the UI: never probed (Unknown), probed-and-fixed (No), movable (Yes)."""
    caps = getattr(cam, "ptz_capabilities", None)
    if not isinstance(caps, dict):
        return "Unknown"
    return "Yes" if caps.get("supported") else "No"


def camera_row(cam: Any, *, zone: str, streaming: Optional[bool], uptime_pct: Optional[float],
               relay_url: str) -> list:
    status = "Unknown" if streaming is None else ("Streaming" if streaming else "Not streaming")
    if not getattr(cam, "enabled", True):
        status = "Disabled"
    sub = getattr(cam, "sub_track", None) or {}
    return [
        cam.name or "", str(cam.id), cam.slug or "", zone,
        status, uptime_pct, _yn(cam.enabled), _yn(cam.recording),
        _schedule_label(getattr(cam, "recording_schedule", None)),
        format_retention(getattr(cam, "retention_minutes", None)),
        cam.ip or "", cam.mac or "", cam.vendor or "", cam.model or "", cam.firmware or "",
        cam.onvif_port, _yn(getattr(cam, "onvif_use_https", None)), cam.rtsp_port,
        strip_credentials(cam.rtsp_url), relay_url, "Yes" if sub.get("url_raw") else "No",
        _motion_label(cam), _search_label(cam), _ptz_label(cam),
        cam.health_status or "", _excel_dt(cam.last_seen_at), _excel_dt(cam.created_at),
        _excel_dt(cam.updated_at), cam.discovery_error or "",
    ]


def build_workbook(rows: Iterable[list], *, generated_by: str, now: Optional[datetime] = None,
                   selection: bool = False) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    rows = list(rows)
    now = now or datetime.now(timezone.utc)
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", start_color="1F2937")
    status_fill = {"Streaming": "C6EFCE", "Not streaming": "FFC7CE", "Disabled": "E7E6E6"}
    tz_name = datetime.now().astimezone().tzname() or "server time"

    wb = Workbook()
    ws = wb.active
    ws.title = "Cameras"
    _append(ws, [h if h not in _LOCAL_DT_HEADERS else f"{h} ({tz_name})" for h in _H])
    for i, (_, width) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=i)
        cell.font, cell.fill = head_font, head_fill
        cell.alignment = Alignment(vertical="center")
        ws.column_dimensions[get_column_letter(i)].width = width
    for r in rows:
        _append(ws, r)
        n = ws.max_row
        fill = status_fill.get(r[STATUS_COL - 1])
        if fill:
            ws.cell(row=n, column=STATUS_COL).fill = PatternFill("solid", start_color=fill)
        for c in _DT_COLS:
            ws.cell(row=n, column=c).number_format = "yyyy-mm-dd hh:mm:ss"
        # 98.5 and 100 in one column read as different kinds of number otherwise.
        ws.cell(row=n, column=_UPTIME_COL).number_format = "0.0"
    # Header + Name stay put while scrolling a 60-camera, 29-column sheet; filters on every column.
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(1, ws.max_row)}"

    # ── Summary ──────────────────────────────────────────────────────────────────────────────────
    s = wb.create_sheet("Summary")
    bold = Font(bold=True)
    statuses = [r[STATUS_COL - 1] for r in rows]
    _append(s, ["Camera inventory"]); s["A1"].font = Font(bold=True, size=14)
    _append(s, ["Generated", _excel_dt(now)]); s["B2"].number_format = "yyyy-mm-dd hh:mm:ss"
    _append(s, ["Generated by", generated_by])
    _append(s, ["Scope", "Selected cameras" if selection else "All cameras"])
    _append(s, ["Cameras", len(rows)])
    for label in ("Streaming", "Not streaming", "Disabled", "Unknown"):
        _append(s, [label, statuses.count(label)])
    _append(s, [])

    def _table(title: str, key_idx: int) -> None:
        _append(s, [title, "Cameras", "Streaming"])
        for c in s[s.max_row]:
            c.font = bold
        counts: dict[str, list[int]] = {}
        for r in rows:
            k = r[key_idx] or "—"
            counts.setdefault(k, [0, 0])
            counts[k][0] += 1
            counts[k][1] += r[STATUS_COL - 1] == "Streaming"
        for k in sorted(counts):
            _append(s, [k, *counts[k]])
        _append(s, [])

    _table("Zone", _H.index("Zone"))
    _table("Model", _H.index("Model"))
    _table("Retention", _H.index("Retention"))
    _append(s, ["Camera usernames and passwords are never exported; RTSP URLs are shown without credentials."])
    s[f"A{s.max_row}"].font = Font(italic=True, color="6B7280")
    s.column_dimensions["A"].width = 30
    s.column_dimensions["B"].width = 22
    s.column_dimensions["C"].width = 12

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
