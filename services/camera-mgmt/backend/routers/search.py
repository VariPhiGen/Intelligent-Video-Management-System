"""search.py — authenticated gateway to the CLIP index, scoped to this VMS.

    /api/search/people    →  <SMARTSEARCH_API_URL>/person/search
    /api/search/vehicles  →  <SMARTSEARCH_API_URL>/vehicles/search
    /api/search/cameras   →  the cameras worth offering as a filter
    /api/search/stats     →  index sizes, and whether the index answers at all

Why a gateway rather than the browser calling the index directly:

* **Scope.** The index is shared with the analytics appliance and holds crops
  from cameras this VMS does not own or record. Filtering in the browser means
  shipping those hits to the client and merely not drawing them; here they never
  leave the appliance. See ``services/search_scope.py`` for the rules.
* **Authorisation.** ``smart_search`` is enforced here, so the capability is a
  real permission rather than a hidden nav item.
* **Audit.** Searching people by appearance is exactly the action the trail
  should carry. Each query records who ran it, what they asked, and how much
  came back.
* **One round trip.** The recorder join (retained window + segment coverage)
  happens next to the recorder instead of as N calls from the operator's
  browser.

The index service has no auth of its own and, on this deployment, answers on a
public name. Moving the browser off it is the prerequisite for locking it down;
narrowing its exposure to the proxying appliances is the follow-up.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from ..db import get_db
from ..models import Camera, CameraStage
from ..security import get_principal
from ..services import audit as audit_svc
from ..services import policy as policy_svc
from ..services import search_scope
from ..services import smartsearch_client as index_client
from ..services.smartsearch_client import SearchUnavailable, is_configured

log = structlog.get_logger(__name__)
router = APIRouter(tags=["search"])

CAPABILITY = "smart_search"


#: Domains whose stored crops this proxy will serve. A LITERAL, because the
#: index's registry lives in another service and this one must not import it —
#: but it has to track it: a domain missing here renders as a broken thumbnail
#: with a 404 behind it, which is exactly how the faces gallery first shipped.
CROP_DOMAINS = ("person", "vehicles", "face")


@router.get("/image")
async def crop_image(domain: str, id: str, request: Request) -> Response:
    """Proxy a stored crop image from the index so the browser can show the
    exact matched crop — same auth + capability as search. Read-only; returns
    the JPEG bytes."""
    principal = await get_principal(request)
    await policy_svc.require(principal, CAPABILITY)
    if domain not in CROP_DOMAINS:
        raise HTTPException(status_code=404, detail="unknown domain")
    if not is_configured():
        raise HTTPException(
            status_code=503,
            detail="Smart Search is not configured on this installation.",
        )
    url = f"{index_client._base()}/{domain}/image"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            resp = await client.get(url, params={"id": id})
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"index unreachable: {exc}")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="crop image not available")
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/frame")
async def detection_frame(domain: str, id: str, request: Request) -> Response:
    """Proxy the whole frame a detection came from. The Recent-detections feed
    draws the object's box over it from the row's bbox; the file itself is
    unmarked.

    Same auth and capability as the crop, because it is the same kind of data
    about the same people — with more of the scene in it, not less. A 404 is
    ordinary (a row older than frames, or a frame that has aged out) and the
    page falls back to the crop."""
    principal = await get_principal(request)
    await policy_svc.require(principal, CAPABILITY)
    if domain not in ("person", "vehicles"):
        raise HTTPException(status_code=404, detail="unknown domain")
    if not is_configured():
        raise HTTPException(
            status_code=503,
            detail="Smart Search is not configured on this installation.",
        )
    url = f"{index_client._base()}/{domain}/frame"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            resp = await client.get(url, params={"id": id})
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"index unreachable: {exc}")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="frame not available")
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "private, max-age=3600"},
    )


EMPTY_DETECTIONS: dict[str, Any] = {
    "scoped": True,
    "cameras": 0,
    "summary": {"total": 0, "by_domain": {}, "by_type": {}, "top_cameras": [],
                "by_hour": [], "plates_read": 0, "distinct_plates": 0,
                "vehicles_seen": 0},
    "recent": [],
    # Present and empty rather than absent: the dashboard distinguishes "no
    # plates in this range" from "this index does not report plates", and an
    # omitted key reads as the second.
    "recent_plates": [],
    "top_plates": [],
}


@router.get("/detections")
async def detections(
    request: Request,
    since_ms: int = 0,
    limit: int = 60,
    tz_offset_min: int = 0,
    domain: list[str] | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """The AI-detections feed + summary (person/vehicle) behind the Analytics →
    Events dashboard, scoped to this VMS's cameras. Same auth/capability as search.

    Not a pass-through, because every figure in it — total, hourly volume, top
    cameras — is aggregated inside the index and cannot be narrowed afterwards.
    Unscoped, the dashboard reported another deployment's activity as this
    site's: the index is shared, and its busiest camera is not ours."""
    principal = await get_principal(request)
    await policy_svc.require(principal, CAPABILITY)

    cameras, recorder = await _searchable(db)
    slugs = [c.slug for c in cameras if c.slug and (recorder is None or c.slug in recorder)]
    if not is_configured():
        raise HTTPException(
            status_code=503,
            detail="Smart Search is not configured on this installation.",
        )
    if not slugs:
        return EMPTY_DETECTIONS

    url = f"{index_client._base()}/detections"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            resp = await client.get(url, params=[
                ("since_ms", since_ms), ("limit", limit), ("tz_offset_min", tz_offset_min),
                *[("camera_ids", s) for s in slugs],
                # Narrows the FEED only. The summary stays over every domain, so
                # the tiles and charts do not move when the operator filters the
                # crop list — see the index's detections() for why.
                *[("domain", d) for d in (domain or []) if d in ("person", "vehicles")],
            ])
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"index unreachable: {exc}")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="detections unavailable")
    body = resp.json()
    if not (isinstance(body, dict) and body.get("scoped")):
        # An index too old to honour the filter answers with every camera it
        # holds. Refusing is the only safe reading: the operator cannot tell
        # from the page that the activity on it belongs to someone else.
        log.warning("search.detections.unscoped", cameras=len(slugs))
        raise HTTPException(
            status_code=502,
            detail="The search index returned an unscoped feed, so it cannot be "
                   "shown here. It needs the update that filters detections by camera.",
        )
    return {**body, "cameras": len(slugs)}

# The index has no "any of these cameras" filter, so the registry rule is
# applied after the fact. Over-fetching leaves headroom for it; without this a
# page of out-of-scope hits would come back empty rather than short.
OVERFETCH = 5
MAX_UPSTREAM_TOP_K = 240


class PeopleQuery(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=24, ge=1, le=96)
    score_threshold: float = Field(default=0.05, ge=0.0, le=1.0)
    # A registry slug. Validated against the searchable set before use, so this
    # cannot be used to probe the index for cameras this VMS does not own.
    camera: Optional[str] = Field(default=None, max_length=255)
    time_from: Optional[str] = Field(default=None, max_length=64)
    time_to: Optional[str] = Field(default=None, max_length=64)
    # Ordering only, applied after relevance, the score floor and the filters.
    # One field arranges the page; the other is not consulted.
    sort_by: str = Field(default="time", pattern="^(time|confidence)$")
    sort_dir: str = Field(default="desc", pattern="^(desc|asc)$")


class PlatesQuery(BaseModel):
    """A plate lookup. No `query`, no `score_threshold`: a plate is an exact
    identifier, and ranking exact matches by visual similarity to a description
    the operator had to invent is noise, not relevance."""
    plate: str = Field(min_length=1, max_length=32)
    camera: Optional[str] = Field(default=None, max_length=255)
    time_from: Optional[str] = Field(default=None, max_length=64)
    time_to: Optional[str] = Field(default=None, max_length=64)
    limit: int = Field(default=200, ge=1, le=2000)


class VehiclesQuery(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=24, ge=1, le=96)
    score_threshold: float = Field(default=0.05, ge=0.0, le=1.0)
    plate: Optional[str] = Field(default=None, max_length=32)
    vehicle_type: Optional[str] = Field(default=None, max_length=32)
    color: Optional[str] = Field(default=None, max_length=32)
    # The same three the person search takes. The form offered the dates for
    # vehicles all along and nothing carried them; the camera was never even
    # offered, so a vehicle search always covered every camera.
    camera: Optional[str] = Field(default=None, max_length=255)
    time_from: Optional[str] = Field(default=None, max_length=64)
    time_to: Optional[str] = Field(default=None, max_length=64)
    # Ordering only, applied after relevance, the score floor and the filters.
    # One field arranges the page; the other is not consulted.
    sort_by: str = Field(default="time", pattern="^(time|confidence)$")
    sort_dir: str = Field(default="desc", pattern="^(desc|asc)$")


async def _registered(db: AsyncSession) -> list[Camera]:
    rows = await db.execute(
        select(Camera).where(Camera.stage == CameraStage.REGISTERED)
    )
    return list(rows.scalars().all())


def _camera_ref(cam: Camera) -> dict[str, Any]:
    """What a result needs to name its camera and deep-link into Playback."""
    return {"id": str(cam.id), "slug": cam.slug, "name": cam.name}


async def _searchable(db: AsyncSession) -> tuple[list[Camera], Optional[dict[str, dict]]]:
    """Registered cameras, and the recorder inventory used to narrow them."""
    cameras = await _registered(db)
    recorder = await search_scope.recorder_cameras()
    return cameras, recorder


@router.get("/cameras")
async def searchable_cameras(
    request: Request, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Cameras a search can return something for: in this registry and known to
    the recorder. Drives the camera filter, so it can only offer choices capable
    of producing a playable hit."""
    principal = await get_principal(request)
    await policy_svc.require(principal, CAPABILITY)

    cameras, recorder = await _searchable(db)
    usable = [c for c in cameras if c.slug and (recorder is None or c.slug in recorder)]
    usable.sort(key=lambda c: (c.name or c.slug or "").lower())
    # `search_indexing` rides along so the SPA can distinguish a camera that
    # found nothing from one that was never indexed. Without it an opted-out
    # camera returns an empty result set that reads as "this person was never
    # here" — the one answer a search must never give wrongly.
    return {
        "cameras": [
            {**_camera_ref(c), "search_indexing": bool(c.search_indexing)}
            for c in usable
        ],
        "recorder_available": recorder is not None,
    }


@router.post("/plates")
async def search_plates(
    body: PlatesQuery, request: Request, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Plate sightings for this VMS's cameras, grouped by plate.

    Same scoping rule as every other search: a sighting is only shown if its
    camera is in this registry AND the recorder still holds that moment, because
    a sighting the operator cannot open in Playback is a dead end.
    """
    principal = await get_principal(request)
    await policy_svc.require(principal, CAPABILITY)

    cameras, recorder = await _searchable(db)
    slugs = [c.slug for c in cameras if c.slug and (recorder is None or c.slug in recorder)]
    if not is_configured():
        raise HTTPException(
            status_code=503,
            detail="Smart Search is not configured on this installation.",
        )
    if body.camera:
        if body.camera not in slugs:
            raise HTTPException(404, detail="Unknown camera")
        slugs = [body.camera]
    if not slugs:
        return {"plates": [], "plates_active": None, "sightings": 0, "cameras": 0}

    try:
        upstream = await index_client.search_plates(
            plate=body.plate, camera_ids=slugs,
            time_from=body.time_from, time_to=body.time_to, limit=body.limit,
        )
    except SearchUnavailable as exc:
        raise HTTPException(exc.status_code, detail=str(exc)) from exc

    groups = list(upstream.get("plates") or [])
    # Flatten to run the registry/retention rule, then regroup: scope() is what
    # guarantees every sighting shown can actually be opened.
    flat = [{**s, "plate": g["plate"]} for g in groups for s in g.get("sightings", [])]
    scoped = await search_scope.scope(
        flat,
        candidates_of=lambda h: (h.get("camera_id"),),
        timestamp_of=lambda h: h.get("timestamp"),
        cameras=cameras,
    )

    regrouped: dict[str, dict[str, Any]] = {}
    for item in scoped.kept:
        h = item.hit
        entry = regrouped.setdefault(h["plate"], {"plate": h["plate"], "sightings": []})
        entry["sightings"].append({
            "id": h.get("id"),
            "camera": _camera_ref(item.camera),
            "when_ms": item.when_ms,
            "vehicle_type": h.get("vehicle_type"),
            "color": h.get("color"),
            "confidence": h.get("confidence"),
        })
    out = []
    for entry in regrouped.values():
        entry["sightings"].sort(key=lambda s: s["when_ms"], reverse=True)
        entry["count"] = len(entry["sightings"])
        entry["last_seen_ms"] = entry["sightings"][0]["when_ms"]
        entry["first_seen_ms"] = entry["sightings"][-1]["when_ms"]
        entry["cameras"] = sorted({s["camera"]["slug"] for s in entry["sightings"]})
        out.append(entry)
    out.sort(key=lambda e: e["last_seen_ms"], reverse=True)

    await audit_svc.record(
        request, principal, "smartsearch.query",
        target="plates",
        detail={"query": body.plate, "returned": sum(e["count"] for e in out),
                "matched": len(flat), "plates": len(out),
                "from": body.time_from, "to": body.time_to},
    )
    return {
        "plates": out,
        # None = the index did not say. False = plate reading is off upstream,
        # which is why there are no results — a different thing from "not seen".
        "plates_active": upstream.get("plates_active"),
        "sightings": sum(e["count"] for e in out),
        "withheld": scoped.dropped,
        "cameras": len(slugs),
    }


@router.get("/stats")
async def index_stats(
    request: Request, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """How much of the index belongs to this VMS's cameras, per domain. Also the
    reachability probe, so the page can say 'index offline' up front instead of
    only failing once someone searches.

    Scoped to the same cameras a search can return, because a count and a result
    set that disagree are worse than no count: the index is shared, and its
    total was overwhelmingly other deployments' rows — a page could advertise
    thousands of entries and then answer every query with nothing."""
    principal = await get_principal(request)
    await policy_svc.require(principal, CAPABILITY)

    cameras, recorder = await _searchable(db)
    slugs = [c.slug for c in cameras if c.slug and (recorder is None or c.slug in recorder)]
    try:
        return {
            "reachable": True,
            "configured": True,
            "cameras": len(slugs),
            "domains": await index_client.stats(slugs),
        }
    except SearchUnavailable as exc:
        return {
            "reachable": False,
            "configured": exc.configured,
            "cameras": len(slugs),
            "domains": {},
            "detail": str(exc),
        }


async def _finish(
    request: Request,
    principal,
    *,
    domain: str,
    query: str,
    top_k: int,
    scoped: search_scope.ScopeResult,
    upstream_count: int,
    extra_detail: dict[str, Any],
    shape,
) -> dict[str, Any]:
    """Trim to the requested page, audit the query, and build the response."""
    kept = scoped.kept
    results = [shape(item) for item in kept[:top_k]]

    await audit_svc.record(
        request, principal, "smartsearch.query",
        target=domain,
        detail={
            "query": query,
            "returned": len(results),
            "matched": upstream_count,
            "withheld": scoped.dropped or None,
            **{k: v for k, v in extra_detail.items() if v not in (None, "")},
        },
    )
    return {
        "results": results,
        # What the index matched but this VMS will not serve, by reason. Shown
        # to the operator so a short page is explainable.
        "withheld": scoped.dropped,
        "matched": upstream_count,
        "truncated": len(kept) > top_k,
    }


@router.post("/people")
async def search_people(
    body: PeopleQuery, request: Request, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    principal = await get_principal(request)
    await policy_svc.require(principal, CAPABILITY)

    cameras, recorder = await _searchable(db)
    slugs = [c.slug for c in cameras if c.slug and (recorder is None or c.slug in recorder)]

    sensor_id: Optional[str] = None
    if body.camera:
        match = next((c for c in cameras if c.slug == body.camera), None)
        if match is None or (recorder is not None and match.slug not in recorder):
            raise HTTPException(404, detail=f"No searchable camera '{body.camera}' in this VMS")
        # The index names a VMS-provisioned camera by its slug, and `sensor_id`
        # is the parameter it honours (see smartsearch_client).
        sensor_id = match.slug

    # Nothing to search: the upstream call could only return other deployments'
    # rows, every one of which the registry rule would then drop.
    if not slugs:
        return await _finish(
            request, principal, domain="people", query=body.query, top_k=body.top_k,
            scoped=search_scope.ScopeResult(), upstream_count=0,
            extra_detail={"camera": body.camera}, shape=lambda item: {},
        )

    # One camera selected → the index filters it exactly, so ask for the page
    # size. Otherwise over-fetch, because the registry rule runs on our side.
    upstream_top_k = body.top_k if sensor_id else min(body.top_k * OVERFETCH, MAX_UPSTREAM_TOP_K)

    try:
        hits = await index_client.search_people(
            query=body.query,
            top_k=upstream_top_k,
            score_threshold=body.score_threshold,
            sensor_id=sensor_id,
            # The registry rule still runs after this; scoping upstream keeps a
            # page of another site's hits from filling the result window and
            # crowding out our own, which no amount of over-fetch would fix.
            sensor_ids=slugs,
            time_from=body.time_from,
            time_to=body.time_to,
            sort_by=body.sort_by,
            sort_dir=body.sort_dir,
        )
    except SearchUnavailable as exc:
        raise HTTPException(exc.status_code, detail=str(exc)) from exc

    scoped = await search_scope.scope(
        hits,
        # Both are tried: a hit whose sensor id this VMS never issued can still
        # be placed by the camera name riding alongside it.
        candidates_of=lambda h: (h.get("sensor_id"), h.get("camera_name")),
        timestamp_of=lambda h: h.get("timestamp"),
        cameras=cameras,
    )

    def shape(item: search_scope.Scoped) -> dict[str, Any]:
        h = item.hit
        return {
            "id": h.get("id"),
            "score": h.get("score"),
            "camera": _camera_ref(item.camera),
            "sensor_id": h.get("sensor_id") or h.get("camera_name"),
            "when_ms": item.when_ms,
            "tracker_id": h.get("tracker_id"),
            "confidence": h.get("confidence"),
            "frame_number": h.get("frame_number"),
            "pad_index": h.get("pad_index"),
            # The card draws this box over the stored frame; `has_frame` says
            # whether there is one, and `sightings` how many observations of
            # this object the index collapsed into this result.
            "bbox": h.get("bbox"),
            "has_frame": h.get("has_frame"),
            "sightings": h.get("sightings"),
        }

    return await _finish(
        request, principal, domain="people", query=body.query, top_k=body.top_k,
        scoped=scoped, upstream_count=len(hits),
        extra_detail={"camera": body.camera, "from": body.time_from, "to": body.time_to},
        shape=shape,
    )


@router.post("/people/by-image")
async def search_people_by_image(
    request: Request,
    top_k: int = Query(default=24, ge=1, le=96),
    score_threshold: float = Query(default=0.15, ge=0.0, le=1.0),
    camera: Optional[str] = Query(default=None, max_length=255),
    time_from: Optional[str] = Query(default=None, max_length=64),
    time_to: Optional[str] = Query(default=None, max_length=64),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Search-by-example: the body is a crop image (raw JPEG bytes — typically a
    box the operator drew on a playback frame), everything else mirrors
    POST /people exactly: same capability, same registry+recorder scoping, same
    audit, same result shape. A different front door into the same index, and
    deliberately nothing more.

    The default threshold is higher-stakes here than for text: CLIP image-to-
    image similarity runs hotter than text-to-image, so 0.15 still returns the
    nearest crops of an unrelated set. The UI surfaces scores; the operator
    judges."""
    principal = await get_principal(request)
    await policy_svc.require(principal, CAPABILITY)

    image = await request.body()
    if not image:
        raise HTTPException(400, detail="Send the query crop as the request body (image/jpeg)")
    if len(image) > 8 * 1024 * 1024:
        raise HTTPException(413, detail="Query image exceeds 8 MB")

    cameras, recorder = await _searchable(db)
    slugs = [c.slug for c in cameras if c.slug and (recorder is None or c.slug in recorder)]

    sensor_ids: Optional[list[str]] = slugs
    if camera:
        match = next((c for c in cameras if c.slug == camera), None)
        if match is None or (recorder is not None and match.slug not in recorder):
            raise HTTPException(404, detail=f"No searchable camera '{camera}' in this VMS")
        sensor_ids = [match.slug]

    if not slugs:
        return await _finish(
            request, principal, domain="people", query="[image]", top_k=top_k,
            scoped=search_scope.ScopeResult(), upstream_count=0,
            extra_detail={"camera": camera}, shape=lambda item: {},
        )

    try:
        hits = await index_client.search_people_by_image(
            image=image,
            # Over-fetch like the text path: the registry rule runs on our side.
            top_k=min(top_k * OVERFETCH, MAX_UPSTREAM_TOP_K),
            score_threshold=score_threshold,
            sensor_ids=sensor_ids,
            time_from=time_from, time_to=time_to,
        )
    except SearchUnavailable as exc:
        raise HTTPException(exc.status_code, detail=str(exc)) from exc

    scoped = await search_scope.scope(
        hits,
        candidates_of=lambda h: (h.get("sensor_id"), h.get("camera_name")),
        timestamp_of=lambda h: h.get("timestamp"),
        cameras=cameras,
    )

    def shape(item: search_scope.Scoped) -> dict[str, Any]:
        h = item.hit
        return {
            "id": h.get("id"),
            "score": h.get("score"),
            "camera": _camera_ref(item.camera),
            "sensor_id": h.get("sensor_id") or h.get("camera_name"),
            "when_ms": item.when_ms,
            "tracker_id": h.get("tracker_id"),
            "confidence": h.get("confidence"),
            "frame_number": h.get("frame_number"),
            "pad_index": h.get("pad_index"),
            # The card draws this box over the stored frame; `has_frame` says
            # whether there is one, and `sightings` how many observations of
            # this object the index collapsed into this result.
            "bbox": h.get("bbox"),
            "has_frame": h.get("has_frame"),
            "sightings": h.get("sightings"),
        }

    return await _finish(
        request, principal, domain="people", query="[image]", top_k=top_k,
        scoped=scoped, upstream_count=len(hits),
        # The audit row says an image query ran and how big the crop was — the
        # crop itself is footage-derived personal data and does not belong in
        # the audit log.
        extra_detail={"camera": camera, "from": time_from, "to": time_to,
                      "image_bytes": len(image)},
        shape=shape,
    )


#: How many photos one face search may combine. A LITERAL for CROP_DOMAINS'
#: reason: the index enforces its own MAX_QUERY_PHOTOS in another service. This
#: must not exceed that one, or a search accepted here is refused behind it.
MAX_FACE_PHOTOS = 5
_MAX_FACE_PHOTO_BYTES = 8 * 1024 * 1024


async def _read_face_photos(request: Request) -> list[bytes]:
    """One photo as the raw body (the original contract), or up to
    MAX_FACE_PHOTOS photos of one person as multipart `photos` parts."""
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        # One file past the limit is parsed so "too many" is reported as that.
        form = await request.form(max_files=MAX_FACE_PHOTOS + 1, max_fields=8)
        try:
            images: list[bytes] = []
            for part in form.getlist("photos"):
                if isinstance(part, str):
                    raise HTTPException(400, detail="Each `photos` part must be a file")
                images.append(await part.read())
        finally:
            await form.close()
    else:
        body = await request.body()
        images = [body] if body else []
    if not images:
        raise HTTPException(
            400, detail="Send the query photo as the request body (image/jpeg), "
                        f"or up to {MAX_FACE_PHOTOS} photos as multipart `photos`")
    if len(images) > MAX_FACE_PHOTOS:
        raise HTTPException(400, detail=f"At most {MAX_FACE_PHOTOS} photos per face search")
    single = len(images) == 1
    for n, image in enumerate(images, start=1):
        if not image:
            raise HTTPException(400, detail=f"Photo {n} is empty")
        if len(image) > _MAX_FACE_PHOTO_BYTES:
            raise HTTPException(
                413, detail="Query image exceeds 8 MB" if single else f"Photo {n} exceeds 8 MB")
    return images


@router.post("/faces/by-image")
async def search_faces_by_image(
    request: Request,
    top_k: int = Query(default=24, ge=1, le=96),
    score_threshold: float = Query(default=0.0, ge=0.0, le=1.0),
    camera: Optional[str] = Query(default=None, max_length=255),
    time_from: Optional[str] = Query(default=None, max_length=64),
    time_to: Optional[str] = Query(default=None, max_length=64),
    min_width_px: int = Query(default=0, ge=0, le=4096),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Find other appearances of the face in an uploaded photograph.

    SEARCH, NOT IDENTIFICATION. There is no watchlist and no name anywhere in
    this path: the answer is "here are stored faces that look like this one",
    ranked, with the scores shown. The operator judges, exactly as they do for
    the person search-by-example this mirrors.

    THE DEFAULT THRESHOLD IS 0.0 ON PURPOSE, which is the opposite of the
    person image search's 0.15. Face scores on this footage are not comparable
    to CLIP's: measured on the live corpus, two photographs of the SAME person
    score a median 0.387 while different people score 0.117, so a threshold
    borrowed from another domain would either hide every true match or admit
    everything. The UI ranks and labels; a floor that means something has to be
    chosen from a site's own numbers, which is why it is a parameter rather
    than a constant.

    ONE PHOTO OR SEVERAL. One is the raw body, as before. Up to MAX_FACE_PHOTOS
    of the same person go as multipart `photos`, and the index pools their
    faces into one query. It reports per photo whether a face was used, and how
    closely the photos agree — two different people averaged together rank
    plausibly and match neither, and only the operator can tell that apart.
    """
    principal = await get_principal(request)
    await policy_svc.require(principal, CAPABILITY)

    images = await _read_face_photos(request)
    image_bytes = sum(len(i) for i in images)

    cameras, recorder = await _searchable(db)
    slugs = [c.slug for c in cameras if c.slug and (recorder is None or c.slug in recorder)]
    camera_ids: Optional[list[str]] = slugs
    if camera:
        match = next((c for c in cameras if c.slug == camera), None)
        if match is None or (recorder is not None and match.slug not in recorder):
            raise HTTPException(404, detail=f"No searchable camera '{camera}' in this VMS")
        camera_ids = [match.slug]

    if not slugs:
        return await _finish(
            request, principal, domain="faces", query="[image]", top_k=top_k,
            scoped=search_scope.ScopeResult(), upstream_count=0,
            extra_detail={"camera": camera}, shape=lambda item: {},
        )

    try:
        body = await index_client.search_faces_by_image(
            images=images,
            top_k=min(top_k * OVERFETCH, MAX_UPSTREAM_TOP_K),
            score_threshold=score_threshold,
            camera_ids=camera_ids,
            time_from=time_from, time_to=time_to, min_width_px=min_width_px,
        )
    except SearchUnavailable as exc:
        raise HTTPException(exc.status_code, detail=str(exc)) from exc

    hits = list(body.get("results") or [])
    scoped = await search_scope.scope(
        hits,
        candidates_of=lambda h: (h.get("camera_id"), h.get("camera_name")),
        timestamp_of=lambda h: h.get("timestamp"),
        cameras=cameras,
    )

    def shape(item: search_scope.Scoped) -> dict[str, Any]:
        h = item.hit
        return {
            "id": h.get("id"),
            "score": h.get("score"),
            "camera": _camera_ref(item.camera),
            "camera_id": h.get("camera_id") or h.get("camera_name"),
            "when_ms": item.when_ms,
            "tracker_id": h.get("tracker_id"),
            "confidence": h.get("confidence"),
            # Carried to the UI so a weak match can be explained by a small
            # face rather than left looking like a bad model.
            "face_width_px": h.get("face_width_px"),
            "bbox": h.get("bbox"),
        }

    out = await _finish(
        request, principal, domain="faces", query="[image]", top_k=top_k,
        scoped=scoped, upstream_count=len(hits),
        # How many photos, never the photos: they are biometric data and do
        # not belong in the audit log any more than the person crop does.
        extra_detail={"camera": camera, "from": time_from, "to": time_to,
                      "photos": len(images), "image_bytes": image_bytes},
        shape=shape,
    )
    # "No face in your photo" travels back intact. It is the one failure the
    # operator can fix themselves, and an empty result set cannot say it.
    out["faces_detected"] = body.get("faces_detected", 0)
    if body.get("detail"):
        out["detail"] = body["detail"]
    if body.get("query_face"):
        out["query_face"] = body["query_face"]
    # Which photo had no usable face, and how closely several photos agree.
    # Facts about the QUERY that only the index can know, passed through as-is.
    for key in ("photos", "photos_used", "agreement"):
        if key in body:
            out[key] = body[key]
    return out


@router.get("/faces/recent")
async def recent_faces(
    request: Request,
    limit: int = Query(default=60, ge=1, le=200),
    camera: Optional[str] = Query(default=None, max_length=255),
    min_width_px: int = Query(default=0, ge=0, le=4096),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """What the face index has collected, newest first.

    The tab opens on this because a domain whose only query is "upload a photo"
    is unusable until the operator has one — and because an empty gallery is
    how a camera that yields no faces tells the truth about itself.
    """
    principal = await get_principal(request)
    await policy_svc.require(principal, CAPABILITY)

    cameras, recorder = await _searchable(db)
    slugs = [c.slug for c in cameras if c.slug and (recorder is None or c.slug in recorder)]
    camera_ids: Optional[list[str]] = slugs
    if camera:
        match = next((c for c in cameras if c.slug == camera), None)
        if match is None or (recorder is not None and match.slug not in recorder):
            raise HTTPException(404, detail=f"No searchable camera '{camera}' in this VMS")
        camera_ids = [match.slug]
    if not slugs:
        return {"results": [], "total": 0}

    try:
        body = await index_client.recent_faces(
            camera_ids=camera_ids, limit=limit, min_width_px=min_width_px)
    except SearchUnavailable as exc:
        raise HTTPException(exc.status_code, detail=str(exc)) from exc

    hits = list(body.get("results") or [])
    scoped = await search_scope.scope(
        hits,
        candidates_of=lambda h: (h.get("camera_id"), h.get("camera_name")),
        timestamp_of=lambda h: h.get("timestamp"),
        cameras=cameras,
    )
    # NOT audited as a query. This is a listing of what the index holds for
    # cameras the operator can already see, the same as the detections feed;
    # audit_svc records the SEARCHES, and drowning that log in tab-opens is how
    # a real query stops being findable in it.
    results = [{
        "id": item.hit.get("id"),
        "camera": _camera_ref(item.camera),
        "camera_id": item.hit.get("camera_id") or item.hit.get("camera_name"),
        "when_ms": item.when_ms,
        "tracker_id": item.hit.get("tracker_id"),
        "confidence": item.hit.get("confidence"),
        "face_width_px": item.hit.get("face_width_px"),
        "bbox": item.hit.get("bbox"),
    } for item in scoped.kept[:limit]]
    return {"results": results, "total": len(results),
            "withheld": scoped.dropped or None}


@router.post("/vehicles")
async def search_vehicles(
    body: VehiclesQuery, request: Request, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    principal = await get_principal(request)
    await policy_svc.require(principal, CAPABILITY)

    cameras = await _registered(db)
    # A named camera narrows the search in the index rather than after it, so
    # the recent window fills with that camera's vehicles instead of being
    # filled by the busiest one and then emptied by the filter. Validated
    # against the registry: an unknown slug is a 404, never an unfiltered
    # search, which is how the retiring index once answered this.
    camera_ids: Optional[list[str]] = None
    if body.camera:
        match = next((c for c in cameras if c.slug == body.camera), None)
        if match is None:
            raise HTTPException(404, detail=f"No searchable camera '{body.camera}' in this VMS")
        camera_ids = [match.slug]
    try:
        hits = await index_client.search_vehicles(
            query=body.query,
            top_k=min(body.top_k * OVERFETCH, MAX_UPSTREAM_TOP_K),
            score_threshold=body.score_threshold,
            plate=body.plate.upper() if body.plate else None,
            vehicle_type=body.vehicle_type,
            color=body.color,
            camera_ids=camera_ids,
            time_from=body.time_from,
            time_to=body.time_to,
            sort_by=body.sort_by,
            sort_dir=body.sort_dir,
        )
    except SearchUnavailable as exc:
        raise HTTPException(exc.status_code, detail=str(exc)) from exc

    scoped = await search_scope.scope(
        hits,
        # Vehicles carry no camera name — `camera_id` is set on the edge ANPR
        # ingest and null on rows scanned out of object storage, which is why
        # most of this domain resolves to nothing and is withheld as unmapped.
        candidates_of=lambda h: (h.get("camera_id"),),
        timestamp_of=lambda h: h.get("timestamp"),
        cameras=cameras,
    )

    def shape(item: search_scope.Scoped) -> dict[str, Any]:
        h = item.hit
        return {
            "id": h.get("id"),
            "score": h.get("score"),
            "camera": _camera_ref(item.camera),
            "sensor_id": h.get("camera_id"),
            "when_ms": item.when_ms,
            "plate": h.get("plate"),
            "confidence": h.get("confidence"),
            "vehicle_type": h.get("vehicle_type"),
            "color": h.get("color"),
            "brand": h.get("brand"),
            "vehicle_speed": h.get("vehicle_speed"),
            "violation": h.get("violation"),
            "tracker_id": h.get("tracker_id"),
            "bbox": h.get("bbox"),
            "has_frame": h.get("has_frame"),
            "sightings": h.get("sightings"),
        }

    return await _finish(
        request, principal, domain="vehicles", query=body.query, top_k=body.top_k,
        scoped=scoped, upstream_count=len(hits),
        extra_detail={"plate": body.plate, "type": body.vehicle_type,
                      "color": body.color, "camera": body.camera,
                      "from": body.time_from, "to": body.time_to},
        shape=shape,
    )
