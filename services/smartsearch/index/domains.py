"""domains.py — ONE definition of what a searchable domain is.

WHY THIS EXISTS. Until faces, this index had two domains and every sweep spelled
them out: `for table in ("search_persons", "search_vehicles")` appears in
expire_once, erase_range, sweep_orphans and health, write_batch branches on
`domain == "person"`, and the API gates on `(PERSON, VEHICLES)`. Adding a third
by editing all of those is precisely the shape of defect the sub-track audit
found eleven times in the NVR: a new per-camera thing arrives, four of five
surfaces learn about it, and the fifth keeps working while quietly meaning
something different.

The one that must never be missed is `erase_range`. An erasure that skips a
table reports success with the data subject still searchable — an erasure
promises footage AND the crops cut from it, and a missed table makes that a
false claim rather than a partial one.

So: a domain is declared once, here, and every surface iterates this table.

THE TWO ODDITIES ARE DELIBERATE AND BOTH ARE LOAD-BEARING.

`camera_column` differs per domain — persons say `sensor_id`, vehicles and faces
say `camera_id` — because the first two mirror the wire contract of the service
this one replaced, and renaming a column to be tidy would break the client that
speaks it.

`dims` differs too: 512 for the CLIP domains, 128 for faces. Faces are NOT
embedded by CLIP. They are embedded by SFace in the service that has the raw
frame, because measuring it (2026-09-14) showed the identity signal between two
photos of one person spans ~0.27 cosine while re-encoding the same face at JPEG
q60 moves it by up to 0.46 — so an embedding taken from the stored q82 crop is
mostly measuring compression. The vector therefore arrives with the observation
instead of being computed here, which is what `embed` records.
"""
from __future__ import annotations

from dataclasses import dataclass, field

PERSON = "person"
VEHICLES = "vehicles"
FACE = "face"

#: How the embedding for a domain is obtained.
EMBED_CLIP = "clip"          # this service encodes the crop
EMBED_SUPPLIED = "supplied"  # the producer sends the vector with the observation


@dataclass(frozen=True)
class Domain:
    #: The name on the wire, in `search_domains`, and in `meta["domain"]`.
    name: str
    table: str
    camera_column: str
    dims: int
    embed: str
    #: Columns beyond the common six, in INSERT order. The row dict keys match.
    extras: tuple[str, ...] = field(default_factory=tuple)
    #: What erase_range calls this domain's count. Spelled out rather than
    #: derived: `persons_deleted` is plural and `vehicles_deleted` already is,
    #: and both are read by camera-mgmt's erasure call and by coverage.py. A
    #: derived name would have renamed one of them and under-reported an
    #: erasure — silently, since a missing key reads as zero.
    result_key: str = ""
    #: Whether rows keep the WHOLE FRAME they came from (`frame_path`,
    #: `frame_boxes`; migrations 005_frame_path and 006_frame_boxes). Person and
    #: vehicle rows do, for the result card and the detections feed. Faces do
    #: not: a face row stores the aligned crop SFace embedded, and the frame of
    #: that moment is already kept by the person row beside it. Every statement
    #: that reads, writes or deletes frame_path asks this first — asking every
    #: table for it is how a sweep fails on search_faces, which has no such
    #: column.
    frames: bool = False

    @property
    def supplies_own_vector(self) -> bool:
        return self.embed == EMBED_SUPPLIED


DOMAINS: dict[str, Domain] = {
    PERSON: Domain(
        name=PERSON, table="search_persons", camera_column="sensor_id",
        dims=512, embed=EMBED_CLIP, extras=("tracker_id",),
        result_key="persons_deleted", frames=True,
    ),
    VEHICLES: Domain(
        name=VEHICLES, table="search_vehicles", camera_column="camera_id",
        dims=512, embed=EMBED_CLIP,
        extras=("vehicle_type", "plate", "plate_confidence", "tracker_id"),
        result_key="vehicles_deleted", frames=True,
    ),
    FACE: Domain(
        name=FACE, table="search_faces", camera_column="camera_id",
        dims=128, embed=EMBED_SUPPLIED,
        # face_width_px is stored because the survey that justified this domain
        # showed retrieval quality tracking face size directly (rank-1 24% under
        # 40px against 40% at 64-111px). Without the column there is no way to
        # apply a quality floor after the fact, or to tell an operator why a
        # camera returns nothing useful.
        # embedding_model is stored per row, which no other domain does. It is
        # what lets a model swap be a re-index instead of a silently mixed
        # index: queries scope to the active model, so half-migrated rows drop
        # out of the answer rather than ranking inside it. See index/faces.py.
        extras=("face_width_px", "tracker_id", "embedding_model"),
        result_key="faces_deleted",
    ),
}

#: Every (table, camera column) pair. Sweeps iterate THIS, never a literal.
TABLES: tuple[tuple[str, str], ...] = tuple(
    (d.table, d.camera_column) for d in DOMAINS.values()
)

#: Every table name, for sweeps that do not need the camera column.
ALL_TABLES: tuple[str, ...] = tuple(d.table for d in DOMAINS.values())

#: The frame columns, in INSERT order, on every domain whose `frames` is True.
FRAME_COLUMNS: tuple[str, ...] = ("frame_path", "frame_boxes")

#: Wire names, in declaration order.
ALL_DOMAINS: tuple[str, ...] = tuple(DOMAINS)


def spec(domain: str) -> Domain:
    """The domain's definition, or KeyError. Callers that take a domain from a
    request should check membership first and answer 404 themselves."""
    return DOMAINS[domain]


def is_domain(value: str) -> bool:
    return value in DOMAINS


#: Every erase_range count key, so a caller can total an erasure without
#: knowing which domains exist. Summing two literals is how faces would have
#: been erased but not reported.
ERASE_KEYS: tuple[str, ...] = tuple(d.result_key for d in DOMAINS.values())


#: The `plate` domain is NOT in DOMAINS above, and that is the distinction this
#: constant exists to keep. A plate is not a table: it is an instruction to read
#: plates off vehicles, stored as a column on the vehicle row. So there are two
#: vocabularies here and they are nearly the same, which is exactly why they
#: have to be named:
#:
#:   DOMAINS / ALL_DOMAINS  what this index STORES — one table each
#:   CAMERA_DOMAINS         what a CAMERA can be configured to contribute
#:
#: Substituting the first for the second rejected every plate camera at
#: registration, with a 422 that read as if `plate` had been withdrawn.
PLATE_MODIFIER = "plate"
CAMERA_DOMAINS: tuple[str, ...] = ALL_DOMAINS + (PLATE_MODIFIER,)
