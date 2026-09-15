"""nested_boxes.py — one object, one box.

THE DEFECT. The detector sometimes returns TWO boxes for one person: a full
body and, nested inside it, the torso alone. Measured on this index —
cam1-h5e0 at 2026-09-09 19:48:40.075, rows 41577 and 41578 — the two boxes
shared an x1 to the pixel, their y1 differed by 4 px, and their heights were
232 and 132. Both were recorded as separate observations of separate objects.

WHY NOTHING DOWNSTREAM CATCHES IT, and this is the whole argument for putting
the fix here:

  * Ultralytics' own NMS does not, because it thresholds on IoU and a nested
    box has a LOW one. For those two boxes IoU = 11352/23664 = 0.48, under the
    0.7 default. NMS is asking "do these cover the same area?"; the question
    that separates a split box from two people is "is one of them ENTIRELY
    inside the other?", which is intersection over the SMALLER area.

  * The tracker cannot, and must not try. Two detections in the same frame are
    certainly two different objects as far as it is concerned — that invariant
    is what stops one track absorbing two people, and it is load-bearing. So a
    split box is GUARANTEED to become two identities.

  * CLIP deduplication cannot. The two crops genuinely look different: one
    contains legs and a carried bag and the other does not. Measured cosine
    for the pair above was 0.7698 against a 0.85 threshold — dedup was right
    by its own rule, and the rule cannot help here.

Which leaves this: the detector boundary, before anything downstream has been
told there were two objects.

THE MEASUREMENT THAT SET THE THRESHOLD. Over 20,714 stored person rows, every
pair of rows sharing a camera and an exact timestamp — so, from one decoded
frame — bucketed by containment:

    containment   pairs   share an edge to within 1% of the frame
      0.0-0.5      6635         31  (0.5%)
      0.5-0.8       335         53  (16%)
      0.8-0.9        92         57  (62%)
      0.9-1.0       420        390  (93%)
      1.0           444        414  (93%)

Two people who happen to overlap do not align an edge; a box cut out of
another box does. The population above 0.9 is 93% edge-aligned and the
population below 0.8 is not, so 0.9 sits in the gap rather than on a slope.

EDGE ALIGNMENT IS NOT PART OF THE TEST, deliberately, even though it is what
makes the population legible. Checked against the 60 pairs above 0.9 that do
NOT share an edge: cam2-6lpf rows 31441/31442 are one man against a wall, once
to the waist (170x303) and once to the ankles (169x392) — the same defect with
the top edges a few pixels apart. Adding an alignment term would have kept
that duplicate for no gain.

THE LARGER BOX WINS, NOT THE MORE CONFIDENT ONE. In that same pair the waist
crop scored 0.65 and the full body 0.51, so confidence would have chosen the
truncated one. Every consumer downstream wants the whole object: CLIP embeds
a person, not a torso; ANPR's best-frame buffer ranks on plate legibility;
an operator reviewing a hit wants to see who it was. A partial crop is worse
at all three, whatever the detector thinks of it.

WHAT THIS IS NOT. It is not deduplication, which compares APPEARANCE across
frames inside a window; and it is not tracking, which compares geometry
BETWEEN frames. This compares geometry WITHIN one frame, which neither of the
other two can see. All three survive because they fail differently.
"""
from __future__ import annotations

from typing import Sequence


def containment(a, b) -> float:
    """Intersection over the SMALLER area, 0..1.

    1.0 means one box lies entirely inside the other. This is `IoS`, not IoU:
    IoU divides by the union and so falls as the size difference grows, which
    is exactly backwards for the case being detected — the more completely a
    torso box sits inside a body box, the LOWER its IoU.
    """
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    iw = min(ax2, bx2) - max(ax1, bx1)
    ih = min(ay2, by2) - max(ay1, by1)
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = float(iw * ih)
    smaller = min(max(1.0, (ax2 - ax1) * (ay2 - ay1)),
                  max(1.0, (bx2 - bx1) * (by2 - by1)))
    return inter / smaller


def suppress_nested(detections: Sequence, threshold: float = 0.9) -> list:
    """Drop boxes that lie inside another box OF THE SAME DOMAIN.

    Returns a new list in the input's order, so nothing downstream can come to
    depend on this having reordered anything.

    PER DOMAIN, AND THAT IS THE ONE RULE THAT MAY NOT BE RELAXED. A person
    standing in a doorway of a bus is legitimately inside the bus's box, and
    is a different object in a different domain — suppressing across domains
    would delete the person and leave the vehicle, which is the one failure
    this codebase treats as unacceptable: a person no search can return.

    `threshold <= 0` disables it, so an operator can turn this off without a
    rebuild and compare.
    """
    if threshold <= 0 or len(detections) < 2:
        return list(detections)

    # Largest first, so the survivor of any nested pair is always the container
    # and one pass is enough: a box can only be suppressed by one already kept.
    order = sorted(
        range(len(detections)),
        key=lambda i: -((detections[i].xyxy[2] - detections[i].xyxy[0])
                        * (detections[i].xyxy[3] - detections[i].xyxy[1])),
    )
    kept: list[int] = []
    dropped: set[int] = set()
    for i in order:
        det = detections[i]
        for j in kept:
            other = detections[j]
            if other.domain != det.domain:
                continue
            if containment(det.xyxy, other.xyxy) >= threshold:
                dropped.add(i)
                break
        else:
            kept.append(i)
    return [d for i, d in enumerate(detections) if i not in dropped]
