"""frame_boxes.py — the frame's complete detection result, as the feed draws it.

Analytics sends, with each observation, every object its frame held: the box,
the label and the tracker id of each. The Recent-detections feed draws all of
them over the one stored picture, so a frame with four people shows four boxes
instead of the one object that observation happened to be about.

WHY IT IS REBUILT HERE FIELD BY FIELD rather than stored as it arrives. It goes
into the database and is then drawn over a picture in an operator's browser, so
its shape is this service's guarantee, not the producer's: coordinates are
clamped to the picture, text is bounded, and anything malformed is dropped
rather than stored and rendered later.

DRAWING ONLY. Nothing here reaches CLIP, the index or dedup — see migrations/006.
"""
from __future__ import annotations

from typing import Any, Optional

PERSON = "person"
VEHICLES = "vehicles"

#: Matches analytics' own cap (pipeline.MAX_FRAME_BOXES). Past this a picture is
#: a wall of labels, and the list is carried on every row of the frame.
MAX_BOXES = 40
#: Labels are detector class names ("person", "truck"); a tracker id is
#: camera:nonce.epoch:counter. Both are short, and neither is trusted to be.
MAX_LABEL = 40
MAX_TRACKER_ID = 128


def clean(raw: Any, limit: int = MAX_BOXES) -> Optional[list[dict]]:
    """Sanitise a box list. None when there is nothing usable to draw."""
    if not isinstance(raw, (list, tuple)):
        return None
    out: list[dict] = []
    for item in raw[:limit]:
        if not isinstance(item, dict):
            continue
        bb = item.get("bbox")
        if not (isinstance(bb, (list, tuple)) and len(bb) == 4):
            continue
        try:
            box = [min(1.0, max(0.0, float(v))) for v in bb]
        except (TypeError, ValueError):
            continue
        # A zero-area or inverted box would draw as a line or vanish; either
        # way it is not a detection anyone can act on.
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        try:
            conf = round(float(item.get("confidence")), 3)
        except (TypeError, ValueError):
            conf = None
        tid = item.get("tracker_id")
        out.append({
            "bbox": [round(v, 5) for v in box],
            "label": str(item.get("label") or "")[:MAX_LABEL],
            # Only the two the product has, so the feed can colour by domain
            # without trusting a string from the wire.
            "domain": PERSON if item.get("domain") == PERSON else VEHICLES,
            "tracker_id": str(tid)[:MAX_TRACKER_ID] if tid else None,
            "confidence": conf,
            # The object this frame was sent for, as against one that was in
            # shot but already recorded inside its interval.
            "recorded": bool(item.get("recorded")),
        })
    return out or None
