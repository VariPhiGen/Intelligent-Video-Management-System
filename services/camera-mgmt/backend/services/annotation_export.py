"""Annotations as COCO.

COCO and not YOLO because the end state is "trained elsewhere": COCO carries
the category NAMES with the data, so a dataset stays self-describing after it
leaves this appliance. YOLO's bare class indices would need the taxonomy
shipped alongside, and would silently mean the wrong thing if the two ever
drifted apart — which they would, the first time a class was added.

Its `segmentation` field is also exactly where a mask would go if one ever
arrives from outside, so nothing here has to change when that happens.

CATEGORIES INCLUDE RETIRED CLASSES. Retirement takes a class out of the picker;
it does not rewrite what was already labelled, and an export that dropped the
category would produce annotations pointing at a category id that is not in the
file.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog
from sqlalchemy import select

from ..models import Annotation, LabelClass

log = structlog.get_logger(__name__)


async def build_coco(
    db,
    *,
    camera: Optional[str] = None,
    class_keys: Optional[list[str]] = None,
    from_dt: Optional[datetime] = None,
    to_dt: Optional[datetime] = None,
) -> dict:
    """A COCO detection document over the selected annotations."""
    classes = (await db.execute(
        select(LabelClass).order_by(LabelClass.sort_order, LabelClass.key))
    ).scalars().all()
    # 1-based: COCO category ids are conventionally 1-based, and a 0 id is
    # read as "no category" by several loaders.
    category_id = {c.key: i for i, c in enumerate(classes, start=1)}

    where = []
    if camera:
        where.append(Annotation.camera_slug == camera)
    if class_keys:
        where.append(Annotation.class_key.in_(class_keys))
    if from_dt:
        where.append(Annotation.captured_at >= from_dt)
    if to_dt:
        where.append(Annotation.captured_at <= to_dt)

    rows = (await db.execute(
        select(Annotation).where(*where)
        .order_by(Annotation.captured_at))).scalars().all()

    images, annotations = [], []
    for i, row in enumerate(rows, start=1):
        images.append({
            "id": i,
            "file_name": Path(row.image_path).name,
            "width": row.frame_width,
            "height": row.frame_height,
            "date_captured": row.captured_at.isoformat(),
            # Provenance travels with the sample: which camera and which
            # instant is what makes an exported frame traceable back to here,
            # and to the footage it was cut from.
            "camera_slug": row.camera_slug,
            "annotation_id": row.id.hex,
        })
        annotations.append({
            "id": i,
            "image_id": i,
            "category_id": category_id[row.class_key],
            # COCO bbox is [x, y, width, height] in IMAGE pixels — which is
            # exactly how it is stored, because AnnotateLayer maps through the
            # letterbox transform before the box ever leaves the browser.
            "bbox": [row.bbox_x, row.bbox_y, row.bbox_w, row.bbox_h],
            "area": row.bbox_w * row.bbox_h,
            "iscrowd": 0,
            "segmentation": [],
        })

    return {
        "info": {"description": "Variphi VMS annotations",
                 "date_created": datetime.now(timezone.utc).isoformat()},
        "licenses": [],
        "categories": [{"id": category_id[c.key], "name": c.key,
                        "supercategory": "none"} for c in classes],
        "images": images,
        "annotations": annotations,
    }
