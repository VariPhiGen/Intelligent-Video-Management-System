"""tracks.py — the one definition of "a camera's recording tracks".

A camera can have two streams worth recording: its main stream, and a
low-resolution sub track (`Camera.sub_track`). Only two surfaces in the product
need to know that — the relay and the NVR — because both project cameras onto
*recording names*. Everything else enumerates `Camera` rows and therefore keeps
seeing exactly one camera per camera: DeepStream sync, motion, the health
monitor, events, permissions, audit. That is the point of storing the sub as a
field rather than a second row, and it is why AI can never bind the low-res
stream by accident.

Both of those surfaces drive off `tracks_for()` rather than reading
`sub_track` themselves. One definition means no surface can silently drift —
the failure mode being a sub that gets recorded but never groomed, or removed
from the relay but left recording in the NVR.

Invariant: **the main track's recording name is the bare slug.** The slug is
already the `nvr_camera_name` join key for detections, events, bookmarks and
audit; renaming it would break all of them. The sub is purely additive.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from ..models import sub_recording_name


@dataclass(frozen=True)
class Track:
    """One recordable stream of one camera."""

    recording_name: str   # what the NVR and the relay call it
    url: str              # RTSP source, credentials included
    kind: str             # "main" | "sub"
    # Relay-only. A sub that exists but is NOT being recorded is carried
    # on-demand: MediaMTX opens it when a viewer asks and closes it after, so
    # it costs nothing until the live view actually needs the fallback.
    on_demand: bool = False

    @property
    def is_sub(self) -> bool:
        return self.kind == "sub"


def sub_track_url(camera: Any) -> str | None:
    """The sub track's RTSP URL with the camera's credentials applied.

    `sub_track["url_raw"]` carries no credentials, exactly like
    `rtsp_candidates` — they are composed at use time from the main URL so
    there is one place secrets live.
    """
    st = getattr(camera, "sub_track", None)
    if not st or not st.get("url_raw"):
        return None
    raw = st["url_raw"]
    main = getattr(camera, "rtsp_url", None) or ""
    # Lift `user:pass@` off the main URL and graft it onto the sub's.
    for scheme in ("rtsps://", "rtsp://"):
        if main.startswith(scheme) and "@" in main[len(scheme):].split("/", 1)[0]:
            creds = main[len(scheme):].split("@", 1)[0]
            for s2 in ("rtsps://", "rtsp://"):
                if raw.startswith(s2):
                    return f"{s2}{creds}@{raw[len(s2):]}"
    return raw


def sub_is_resolved(camera: Any) -> bool:
    """Whether a sub track EXISTS to fall back to — regardless of recording.

    Deliberately distinct from :func:`sub_is_recordable`. One flag was
    answering two unrelated questions and the cheap one was paying the
    expensive one's price:

      * record the sub — a real cost (a second ffmpeg, ~10-25% more disk), so
        rightly opt-in per camera;
      * relay the sub — nearly free on demand, and the ONLY way the live view
        can show an HEVC camera at all, because browsers cannot decode HEVC
        and the fallback in frontend-react/src/pages/live/useHls.ts attaches to
        `<slug>_sub` instead.

    Gating the relay on `recording_enabled` meant that fallback had nothing to
    attach to on every camera whose sub was merely discovered: no `_sub` path
    existed in MediaMTX, so an H.265 camera with a perfectly good H.264 sub
    still showed "Signal lost".
    """
    st = getattr(camera, "sub_track", None)
    return bool(st and st.get("url_raw"))


def sub_is_recordable(camera: Any) -> bool:
    """Whether this camera's sub track should currently be recorded.

    Resolution stores what exists; this decides what runs. A sub that is merely
    *present* is not enough — it has to be switched on, because recording one
    is a real cost (a second ffmpeg, a second relay path, ~10-25% more disk).
    """
    st = getattr(camera, "sub_track", None)
    return bool(st and st.get("recording_enabled") and st.get("url_raw"))


# What an OPERATOR sets on a sub track, as opposed to what a probe discovers.
# Resolution describes what the camera offers; these describe what a person
# decided, and a probe has no opinion about either — so a re-probe must carry
# them across rather than replace them.
OPERATOR_SET_FIELDS = ("recording_enabled", "retention_days")


def carry_operator_settings(prior: dict | None, resolved: dict | None) -> dict | None:
    """Fold an operator's choices from `prior` onto a freshly probed `resolved`.

    `resolve()` always reports `recording_enabled: False` and no retention,
    because it describes the camera rather than the deployment. Re-checking a
    camera therefore overwrote both. `recording_enabled` was being restored by
    hand at the one call site and `retention_days` was not, so a deliberate
    14-day sub retention silently reverted to the 3-day default and footage
    someone had chosen to keep started ageing out.

    Anything an operator can set on a sub track belongs in OPERATOR_SET_FIELDS.
    Keeping the list here rather than at the call site is the point: the next
    field added is set in the same module that defines what a track is.
    """
    if not resolved:
        return resolved
    for key in OPERATOR_SET_FIELDS:
        if (prior or {}).get(key) is not None:
            resolved[key] = prior[key]
    return resolved


def sub_retention_days(camera: Any, main_retention: int | None) -> int:
    """How long to keep this camera's sub track.

    Precedence: the camera's own `sub_track.retention_days`, else the appliance
    default, and never longer than the main — a scrub track outliving the
    footage it is meant to help scrub is pure waste.
    """
    from ..config import settings

    st = getattr(camera, "sub_track", None) or {}
    want = st.get("retention_days") or settings.nvr_sub_retention_days
    effective_main = main_retention or settings.nvr_default_retention_days
    return max(1, min(int(want), int(effective_main)))


def tracks_for(camera: Any) -> list[Track]:
    """Every track that should exist for `camera`, main first.

    Main is always present (it is the camera's identity). The sub appears only
    when it is resolved AND enabled — so turning the flag off makes it vanish
    from every sync surface at once, and the next reconcile tears it down.
    """
    tracks: list[Track] = []
    main_url = getattr(camera, "rtsp_url", None)
    slug = getattr(camera, "slug", None)
    # No main means a half-registered camera. Recording its sub alone is never
    # right — the sub is defined relative to the main, and its credentials are
    # composed from the main's URL, so there is nothing coherent to record.
    if not (slug and main_url):
        return tracks
    tracks.append(Track(recording_name=slug, url=main_url, kind="main"))
    if sub_is_recordable(camera):
        url = sub_track_url(camera)
        if url:
            tracks.append(Track(recording_name=sub_recording_name(slug),
                                url=url, kind="sub"))
    return tracks


def relay_tracks_for(camera: Any) -> list[Track]:
    """Every track the RELAY should carry — a superset of :func:`tracks_for`.

    The relay carries a resolved sub whether or not it is recorded, because the
    live view falls back to it for cameras the browser cannot decode. Recording
    stays opt-in: the NVR keeps driving off `tracks_for`, so nothing here makes
    a sub start recording.
    """
    tracks = tracks_for(camera)
    if any(t.is_sub for t in tracks):
        return tracks                      # already recorded; carried always-on
    if not sub_is_resolved(camera):
        return tracks
    slug = getattr(camera, "slug", None)
    url = sub_track_url(camera)
    if not (slug and url and tracks):
        return tracks
    return tracks + [Track(recording_name=sub_recording_name(slug), url=url,
                           kind="sub", on_demand=True)]


def recording_names(cameras: Iterable[Any]) -> set[str]:
    """Every recording name the fleet should have — for reconcile diffs.

    A reconcile that compared only slugs would see `<slug>_sub` as an orphan and
    delete footage that is being recorded on purpose.

    **Not the right set for anything that judges RELAY paths** — use
    :func:`relay_names` there. The relay carries strictly more than the recorder
    does, and the difference is a real path with a real puller behind it.
    """
    names: set[str] = set()
    for cam in cameras:
        for t in tracks_for(cam):
            names.add(t.recording_name)
    return names


def relay_names(cameras: Iterable[Any]) -> set[str]:
    """Every path name the RELAY may legitimately hold — a superset of
    :func:`recording_names`.

    The two differ by exactly one thing and it matters: a sub that is RESOLVED
    but not recorded. The relay carries it on demand so the live view has
    something to fall back to on a camera whose codec the browser cannot decode
    (see :func:`relay_tracks_for`); the recorder does not, because recording it
    is a real cost and stays opt-in.

    Anything that decides whether a relay path is legitimate has to ask this
    question, not the recorder's. Asking the recorder's classified every
    resolved-but-unrecorded sub as an orphan: `substream.resolve()` always
    stores `recording_enabled: False`, so that is the DEFAULT state of every
    camera with a discovered sub. The health monitor's orphan sweep then deleted
    the path ~17 ms after each reconcile re-added it, once every poll, forever —
    which killed the H.265 -> H.264 fallback the resolved sub exists for, with
    nothing on any screen to say so.
    """
    names: set[str] = set()
    for cam in cameras:
        for t in relay_tracks_for(cam):
            names.add(t.recording_name)
    return names
