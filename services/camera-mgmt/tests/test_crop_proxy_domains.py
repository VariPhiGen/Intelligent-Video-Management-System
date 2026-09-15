"""The crop proxy has to serve every domain the UI can show a crop for.

WHY THIS FILE EXISTS. The faces gallery shipped with every thumbnail blank. Two
causes, one of them here: `/api/search/image` filtered on a literal
`("person", "vehicles")`, so `domain=face` was a 404 behind an <img> that could
only render as an empty tile. Nothing logged an error the operator could see;
the page looked like a black grid.

The other cause was in the browser and is fixed there (an <img src> cannot
carry a bearer token — see FacesPanel's import of CropThumb). This half is
asserted here, over HTTP, because the domain filter runs inside the routed
handler and a direct call to the function would walk past the auth that made
the first symptom a 401 rather than a 404.

Run from anywhere: python -m pytest services/camera-mgmt/tests/test_crop_proxy_domains.py -q
(it used to pass only from services/camera-mgmt — see _smartsearch_unconfigured).
"""
from __future__ import annotations

import pytest

from backend.routers import search as search_router  # noqa: E402

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _smartsearch_unconfigured(monkeypatch):
    """Pin Smart Search to "not configured" for this file.

    WHY THIS EXISTS. `is_configured()` reads settings whose `env_file=".env"`
    resolves against the CWD, so this file's result depended on where pytest
    was started: from `services/camera-mgmt` there is no `.env`, the proxy
    stops at the 503 and the tests pass — but from the REPO ROOT, which is how
    TESTING.md says CI runs them, the appliance's own `.env` supplies
    SMARTSEARCH_API_URL, the proxy tries to reach 127.0.0.1:8013 and conftest's
    network guard fails all three.

    The domain filter is what this file is about, and it runs BEFORE the
    configured check. Pinning the check False makes the 503 the deterministic
    outcome the assertions below already claim it is, and keeps the test off
    the network on a box where the index happens to be up.
    """
    monkeypatch.setattr(search_router, "is_configured", lambda: False)


@pytest.mark.parametrize("domain", ["person", "vehicles", "face"])
async def test_the_proxy_recognises_every_domain_that_stores_crops(client, mint, domain):
    """Not a 404 "unknown domain". Smart Search is pinned unconfigured above,
    so the honest outcome here is 503 — what matters is that the request got
    PAST the domain filter, which is the thing that was wrong."""
    r = await client.get(f"/api/search/image?domain={domain}&id=1",
                         headers=mint(["admin"]))
    assert r.status_code != 404 or "unknown domain" not in r.text, \
        f"the proxy refuses domain={domain}: every crop for it renders blank"


async def test_an_invented_domain_is_still_refused(client, mint):
    """The filter must stay a filter. Widening it to 'anything' would proxy
    arbitrary paths into the index service."""
    r = await client.get("/api/search/image?domain=wombats&id=1",
                         headers=mint(["admin"]))
    assert r.status_code == 404
    assert "unknown domain" in r.text
