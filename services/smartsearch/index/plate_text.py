"""plate_text.py — comparing plate strings, with no model in sight.

WHY THIS EXISTS SEPARATELY. Plate READING moved to the analytics service along
with the rest of detection; plate MATCHING did not, because a search for
"MH12AB1234" has to normalise the typed query the same way the stored text was
normalised, and that is pure string handling with no ONNX runtime behind it.

Leaving it in plates.py would have meant Smart Search importing a module whose
first job is to load a localiser and a recogniser — pulling the whole ANPR
dependency set back into an image built specifically not to carry it.

The rule is deliberately blunt: uppercase, then drop everything that is not a
letter or a digit. Plates are written with spaces, hyphens and dots that vary
by region, by operator and by whoever typed the query, and none of those
variations mean anything.
"""
from __future__ import annotations

import re

#: Anything that is not a letter or a digit is noise for matching purposes.
_KEEP = re.compile(r"[^A-Z0-9]")


def normalise(raw: str) -> str:
    """Uppercase, alphanumeric only. Safe on None and on empty strings."""
    return _KEEP.sub("", (raw or "").upper())
