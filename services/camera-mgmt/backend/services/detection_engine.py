"""detection_engine.py — which engine does a camera's AI: CPU analytics or DeepStream.

ONE DEFINITION, ASKED BY EVERYONE. The switch is DEEPSTREAM_SYNC_ENABLED.

  off (default)  CPU analytics does everything, exactly as before: detects and
                 crops for Smart Search (person, vehicle, face, ANPR), reports
                 playback presence, runs the CPU Activity Engine.
  on             the DeepStream pipeline does ALL of it and posts the results
                 back to this API — search observations
                 (/api/search/observations), playback presence
                 (/api/playback/timeline/presence) and activity events
                 (/api/analytics/events). CPU analytics is handed no cameras,
                 so it idles and releases its models.

Motion markers are neither: the motion service's frame differencing is cheap
and stays where it is in both modes.

Who may WRITE follows the same answer: presence ingest refuses a person /
vehicle segment from the engine that does not own the camera, and the
observation ingest refuses DeepStream while CPU is the engine, so the two can
never both report one object.

Everything here is a pure function of settings and the camera row, so each
rule is testable without a pipeline, an analytics service or a database.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from ..config import settings

#: Search domain → presence kind, for the domains that have a timeline row.
PRESENCE_KIND = {"person": "person", "vehicles": "vehicle"}

#: Domains an observation may carry. `plate` is not one: a plate is read off a
#: vehicle and travels as fields of that vehicle's observation.
OBSERVATION_DOMAINS = ("person", "vehicles", "face")


def deepstream_mode() -> bool:
    """Is DeepStream the engine? Off unless DEEPSTREAM_SYNC_ENABLED is true."""
    return bool(settings.deepstream_sync_enabled)


def engine() -> str:
    return "deepstream" if deepstream_mode() else "cpu"


def _domains(domains: Optional[Iterable[str]]) -> list[str]:
    return [d for d in (domains or []) if d]


def indexed(indexing: bool, domains: Optional[Iterable[str]]) -> bool:
    """Smart Search's own rule: no domain ticked is the same as not indexed."""
    return bool(indexing and _domains(domains))


def _has_activities(cfg: Any) -> bool:
    cfg = cfg if isinstance(cfg, dict) else {}
    return any(isinstance(a, dict) and a.get("type") for a in cfg.get("activities") or [])


def _has_regions_or_activities(cfg: Any) -> bool:
    cfg = cfg if isinstance(cfg, dict) else {}
    return bool(cfg.get("regions") or cfg.get("activities"))


#: The activity key the heatmap overlay is configured under. Spelled here
#: rather than imported from services/heatmaps.py so this module stays a pure
#: function of settings and the camera row — it is imported by the envelope
#: builder, the push and both ingest doors, and none of them should have to
#: pull in the heatmap storage layer to ask a question about configuration.
HEATMAP_ACTIVITY = "Heatmap_overlay"

#: People Distribution: zone occupancy. Its own activity and its own flag, for
#: the same reason the heatmap has one -- a camera can run either without the
#: other, and neither implies the other's endpoint.
DISTRIBUTION_ACTIVITY = "people_distribution"


def heatmap_configured(cfg: Any) -> bool:
    """Does this camera have the heatmap overlay activity configured?"""
    cfg = cfg if isinstance(cfg, dict) else {}
    return any(isinstance(a, dict) and str(a.get("type")) == HEATMAP_ACTIVITY
               for a in cfg.get("activities") or [])


def distribution_configured(cfg: Any) -> bool:
    """Does this camera have the People Distribution activity configured?"""
    cfg = cfg if isinstance(cfg, dict) else {}
    return any(isinstance(a, dict) and str(a.get("type")) == DISTRIBUTION_ACTIVITY
               for a in cfg.get("activities") or [])


# ── CPU analytics ─────────────────────────────────────────────────────────────

def cpu_wants(enabled: bool, indexing: bool, domains, analytics_config) -> bool:
    """Does CPU analytics consume this camera? Never while DeepStream is the engine."""
    if not enabled or deepstream_mode():
        return False
    return indexed(indexing, domains) or _has_activities(analytics_config)


# ── DeepStream ────────────────────────────────────────────────────────────────

def deepstream_wants(camera: Any) -> bool:
    """Does this camera belong on the pipeline?

    Registered and enabled, with something for the pipeline to do: zones or
    activities drawn (the rule the config push always had), or — DeepStream
    being the engine — Smart Search indexing to detect for.
    """
    from ..models import CameraStage

    if getattr(camera, "stage", None) != CameraStage.REGISTERED.value \
            or not getattr(camera, "enabled", False):
        return False
    if _has_regions_or_activities(getattr(camera, "analytics_config", None)):
        return True
    return deepstream_mode() and indexed(getattr(camera, "search_indexing", False),
                                         getattr(camera, "search_domains", None))


def callback_base() -> str:
    """The VMS API root as the pipeline must call it."""
    explicit = (settings.deepstream_callback_url or "").strip().rstrip("/")
    return explicit or f"http://127.0.0.1:{settings.api_port}"


def ingest_block(camera: Any) -> dict:
    """What the pipeline must report back for this camera — the `vms_ingest`
    block of its envelope (see DEEPSTREAM-VMS-INGEST.md). Empty lists mean
    nothing to report on that channel.
    """
    doms = (_domains(getattr(camera, "search_domains", None))
            if indexed(getattr(camera, "search_indexing", False),
                       getattr(camera, "search_domains", None)) else [])
    base = callback_base()
    return {
        "engine": "deepstream",
        # Smart Search: person / vehicles / face observations, plates read off
        # vehicles when "plate" is listed.
        "search_domains": doms,
        # Playback presence: first-to-last sighting of each confirmed track.
        "presence_kinds": [PRESENCE_KIND[d] for d in doms if d in PRESENCE_KIND],
        # This camera's activities are the pipeline's to run and report.
        "activities": _has_activities(getattr(camera, "analytics_config", None)),
        # Heatmap overlay: whether THIS camera has the activity configured, and
        # therefore whether the pipeline should render and post overlays for it.
        # A separate flag from `activities` on purpose — a heatmap is a picture,
        # not an event, it goes to its own endpoint, and it is the one activity
        # whose output the Events page must never receive.
        "heatmap": heatmap_configured(getattr(camera, "analytics_config", None)),
        "distribution": distribution_configured(getattr(camera, "analytics_config", None)),
        "endpoints": {
            "observations": f"{base}/api/search/observations",
            "heartbeat": f"{base}/api/search/producers/deepstream/heartbeat",
            "presence": f"{base}/api/playback/timeline/presence",
            "events": f"{base}/api/analytics/events",
            "heatmaps": f"{base}/api/analytics/heatmaps",
            "distribution": f"{base}/api/analytics/people-distribution",
        },
    }


# ── Who may write ─────────────────────────────────────────────────────────────

def presence_source_ok(camera: Any, kind: str, source: str) -> bool:
    """May `source` report presence of `kind` for this camera?

    Motion comes from the motion service. Person / vehicle comes from the
    engine — never both, so one object is never counted twice. (Whether the
    camera produces `kind` at all is checked separately.)
    """
    if kind == "motion":
        return source == "motion"
    return source == engine()


def heatmap_source_ok(camera: Any, source: str) -> bool:
    """May `source` write heat sessions and overlay pictures for this camera?

    THE SAME ONE-ENGINE RULE presence obeys, for the same reason. `Heatmap_overlay`
    exists on both sides -- the CPU Activity Engine runs it today and the pipeline
    can run it too -- so without this a camera handed from one to the other keeps
    accepting pictures from the engine that no longer owns it. Two producers would
    then mint session ids for the same floor and interleave two accumulations of
    it, each new id convincing `_close_superseded` that the other had finished.
    The operator would see one camera's history flicker between two heatmaps.

    NOT A CONFIGURATION CHECK. A producer that has just lost the activity still
    has a session to close, and the close is how it says so; refusing that would
    strand the session until the stale sweep noticed. Whether the camera has the
    activity at all is `heatmap_configured`, asked where it matters.
    """
    return source == engine()


def observation_refusal(camera: Any, domain: str, has_plate: bool) -> Optional[str]:
    """Why a DeepStream observation for this camera must be refused, or None.

    The observation ingest is DeepStream's door into Smart Search. It is shut
    while CPU is the engine, and it admits only what AI Config asks of the
    camera: an indexed domain, and a plate only when ANPR is ticked.
    """
    if not deepstream_mode():
        return "deepstream_not_engine"
    doms = _domains(getattr(camera, "search_domains", None))
    if not indexed(getattr(camera, "search_indexing", False), doms):
        return "camera_not_indexed"
    if domain not in OBSERVATION_DOMAINS or domain not in doms:
        return "domain_not_indexed"
    if has_plate and (domain != "vehicles" or "plate" not in doms):
        return "plate_not_indexed"
    return None
