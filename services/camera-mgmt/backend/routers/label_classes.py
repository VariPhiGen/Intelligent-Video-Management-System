"""The fleet label taxonomy — what an operator may call the box they drew.

RETIRE, DO NOT DELETE. Annotations name a class by key and outlive an
administrator's change of mind, so deletion is refused while anything
references the class and `active = false` is the supported way to take one out
of circulation. This mirrors how `analytics_activity_types` keeps
`unregistered` rows rather than dropping them (migration 036). Migration 047's
FK is what makes it a rule rather than a convention; the 409 below is the same
rule stated where a human can read it.

The Label Studio snippet at the end is deliberately a SNIPPET and not an API
call. `labelstudio_client.import_frames` refuses to create projects because one
it created would carry the wrong label config; pushing a config from here would
walk straight back into that. So the taxonomy is rendered for a human to paste,
and their project stays theirs.

WRITES ARE ADMIN. Deciding what the fleet is allowed to call a box is
administration, and that has not changed.

READS ARE NOT, and the earlier rule that they were was a bug rather than a
policy. `POST /annotations` is gated on `playback_search` precisely because
drawing a box is an operator action — but the picker an operator chooses the
class from is fed by the list route below. With that route admin-only, a
non-admin loaded an empty taxonomy, was told by the annotate bar that no
classes existed, and could not save at all: the write was open to them and the
read they needed in order to perform it was not.

So the list is gated on holding EITHER capability that implies needing it —
`playback_search` (draw one on Playback) or `data_collection` (manage the pile
on the Data Collection page). Either, not both: the default policy grants
`data_collection` only to supervisor, who also holds `playback_search`, but a
custom policy may separate them and neither half should lose the taxonomy.

A viewer holds neither and still cannot read it, which is correct — they
cannot draw, so the vocabulary for drawing is not theirs to see.

The Label Studio snippet stays admin: it configures somebody's external
project, which is administration however it is rendered.
"""
from __future__ import annotations

from xml.sax.saxutils import quoteattr

import structlog
from fastapi import (APIRouter, Depends, HTTPException, Request, Response,
                     status)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import (
    Annotation,
    LabelClass,
    LabelClassCreateBody,
    LabelClassResponse,
    LabelClassUpdateBody,
)
from ..security import Principal, get_principal, require_role
from ..services import audit as audit_svc
from ..services import policy as policy_svc

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/label-classes", tags=["label-classes"])

_ADMIN = require_role("admin")


async def _taxonomy_reader(request: Request) -> Principal:
    """Hold either capability that implies needing the class list.

    `require_capability` takes one capability and there is no any-of factory,
    so this states the disjunction directly rather than inventing one for a
    single caller. `policy.allowed` short-circuits for admin / service / dev,
    so no special case for administrators is needed here.

    The failure path deliberately defers to `policy.require` rather than
    raising its own 403: that produces the same sentence every other
    capability-gated route produces, naming a permission an administrator can
    actually find and grant.
    """
    principal = await get_principal(request)
    for capability in ("playback_search", "data_collection"):
        if await policy_svc.allowed(principal, capability):
            return principal
    await policy_svc.require(principal, "playback_search")
    return principal  # unreachable: require() raises


async def _counts(db: AsyncSession) -> dict[str, int]:
    rows = await db.execute(
        select(Annotation.class_key, func.count())
        .group_by(Annotation.class_key))
    return {key: n for key, n in rows.all()}


@router.get("", response_model=list[LabelClassResponse])
async def list_classes(db: AsyncSession = Depends(get_db),
                       principal: Principal = Depends(_taxonomy_reader)):
    counts = await _counts(db)
    rows = (await db.execute(
        select(LabelClass).order_by(LabelClass.sort_order, LabelClass.key))
    ).scalars().all()
    return [LabelClassResponse.from_row(r, counts.get(r.key, 0)) for r in rows]


@router.post("", response_model=LabelClassResponse,
             status_code=status.HTTP_201_CREATED)
async def create_class(body: LabelClassCreateBody, request: Request = None,
                       principal: Principal = Depends(_ADMIN),
                       db: AsyncSession = Depends(get_db)):
    if await db.get(LabelClass, body.key):
        raise HTTPException(409, f"A class with key '{body.key}' already exists")
    # `active` is set here rather than left to the column default. The
    # response is built from this in-memory row, and a client-side default only
    # materialises once the session refreshes it back out of Postgres — so
    # relying on it makes the response shape depend on session settings.
    # A class is created active; that is a product fact, not a storage one.
    row = LabelClass(active=True, **body.model_dump())
    db.add(row)
    await db.commit()
    await audit_svc.record(request, principal, "label_class.created",
                           target=body.key, detail={"label": body.label})
    return LabelClassResponse.from_row(row, 0)


@router.patch("/{key}", response_model=LabelClassResponse)
async def update_class(key: str, body: LabelClassUpdateBody,
                       request: Request = None,
                       principal: Principal = Depends(_ADMIN),
                       db: AsyncSession = Depends(get_db)):
    row = await db.get(LabelClass, key)
    if row is None:
        raise HTTPException(404, f"No class with key '{key}'")
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(row, field, value)
    await db.commit()
    # Retiring is the load-bearing edit — it is what an administrator does
    # INSTEAD of deleting — so it gets its own action rather than hiding inside
    # a generic update in the audit log.
    action = ("label_class.retired"
              if changes.get("active") is False else "label_class.updated")
    await audit_svc.record(request, principal, action, target=key,
                           detail=changes or None)
    counts = await _counts(db)
    return LabelClassResponse.from_row(row, counts.get(key, 0))


@router.delete("/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_class(key: str, request: Request = None,
                       principal: Principal = Depends(_ADMIN),
                       db: AsyncSession = Depends(get_db)):
    row = await db.get(LabelClass, key)
    if row is None:
        raise HTTPException(404, f"No class with key '{key}'")
    used = await db.scalar(
        select(func.count()).select_from(Annotation)
        .where(Annotation.class_key == key))
    if used:
        raise HTTPException(
            409,
            f"{used} annotation(s) name '{key}'. Retire the class instead "
            f"(set active = false) — deleting it would orphan them.")
    await db.delete(row)
    await db.commit()
    await audit_svc.record(request, principal, "label_class.deleted", target=key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/label-config")
async def label_config(db: AsyncSession = Depends(get_db),
                       principal: Principal = Depends(_ADMIN)):
    """The taxonomy as a Label Studio labelling config, for pasting.

    Never pushed over the API — see this module's header. Attribute values go
    through quoteattr because this XML is parsed by somebody else's tool, and a
    config that does not parse is a support ticket rather than a feature.
    """
    rows = (await db.execute(
        select(LabelClass).where(LabelClass.active.is_(True))
        .order_by(LabelClass.sort_order, LabelClass.key))).scalars().all()
    labels = "\n    ".join(
        f"<Label value={quoteattr(r.key)} background={quoteattr(r.color)}/>"
        for r in rows)
    xml = (
        "<View>\n"
        '  <Image name="image" value="$image"/>\n'
        '  <RectangleLabels name="label" toName="image">\n'
        f"    {labels}\n"
        "  </RectangleLabels>\n"
        "</View>"
    )
    return {"xml": xml, "class_count": len(rows)}
