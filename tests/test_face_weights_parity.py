"""The face-weights fetcher exists twice and must stay one file.

Both analytics and smartsearch need the OpenCV Zoo face models, both mount the
same `search_models` volume, and neither can import the other: they are
separate images with separate build contexts, so there is nowhere shared to put
this. The answer is a copy — and a copy of a downloader that verifies digests
is exactly the kind of duplication that rots quietly. A digest updated in one
service and not the other does not fail any test in either service; it fails on
an appliance, as a model that silently never installs.

So the copies are asserted identical here, at the root, where a cross-service
claim can actually be checked. If this fails, copy one over the other — do not
"fix" them separately.

No dependencies, no docker: just two files.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPIES = (
    ROOT / "services" / "smartsearch" / "index" / "face_weights.py",
    ROOT / "services" / "analytics" / "analytics" / "face_weights.py",
)


def test_both_services_carry_the_fetcher():
    for path in COPIES:
        assert path.is_file(), f"{path.relative_to(ROOT)} is missing"


def test_the_two_copies_are_identical():
    first, second = (p.read_text(encoding="utf-8") for p in COPIES)
    assert first == second, (
        "services/smartsearch/index/face_weights.py and "
        "services/analytics/analytics/face_weights.py have diverged. They are "
        "one file kept in two build contexts — copy one over the other.")


def test_the_digests_are_pinned_and_look_like_sha256():
    # The fetch is only as safe as these. A placeholder or a truncated digest
    # would turn "verified download" into "download".
    import re
    text = COPIES[0].read_text(encoding="utf-8")
    digests = re.findall(r'"([0-9a-f]{64})"', text)
    assert len(digests) >= 2, "expected a pinned sha256 per shipped model"
