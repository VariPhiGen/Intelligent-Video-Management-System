"""Unit tests for Smart Search erasure (backend/services/smartsearch_client.py).

Erasure is the one call in this module whose failure mode is a false legal
statement rather than a degraded feature, so what is pinned here is mostly the
ways it is allowed to FAIL. Every case below was reachable before these tests
existed, and the two that matter most look like success from every screen in
the product:

  * an index that erased its rows but could not unlink the crop JPEGs, and
  * an index that has no erasure endpoint at all (an older build, or the
    retiring clip-service), which answers 404 and would round up to "done".

Run (from services/camera-mgmt): python -m pytest tests -q
"""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import settings  # noqa: E402
from backend.services import smartsearch_client as sc  # noqa: E402


@pytest.fixture
def urls(monkeypatch):
    """Set the two index URLs; both empty by default."""
    def _set(index_url="", api_url=""):
        monkeypatch.setattr(settings, "smartsearch_index_url", index_url)
        monkeypatch.setattr(settings, "smartsearch_api_url", api_url)
    return _set


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = b"{}"
        self.text = str(payload)

    def json(self):
        return self._payload


def fake_client(monkeypatch, handler):
    """Replace httpx.AsyncClient so `async with ... as c: await c.delete(...)` works."""
    calls = []

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def delete(self, url, params=None, headers=None):
            calls.append({"url": url, "params": params})
            return handler(url)

    monkeypatch.setattr(sc.httpx, "AsyncClient", _Client)
    return calls


# ── target selection ─────────────────────────────────────────────────────────

def test_no_index_configured_yields_no_targets(urls):
    urls()
    assert sc._erase_targets() == []


def test_same_url_for_read_and_write_is_one_target(urls):
    """The usual deployment: this VMS feeds and queries the same local index."""
    urls(index_url="http://127.0.0.1:8013", api_url="http://127.0.0.1:8013")
    assert sc._erase_targets() == ["http://127.0.0.1:8013"]


def test_trailing_slash_does_not_split_one_index_into_two(urls):
    urls(index_url="http://127.0.0.1:8013/", api_url="http://127.0.0.1:8013")
    assert sc._erase_targets() == ["http://127.0.0.1:8013"]


def test_split_deployment_erases_both_indexes(urls):
    """Feeding one index and querying another means BOTH hold the person.

    Erase only the fed one and the crops leave this appliance's disk while the
    subject stays findable in the product; erase only the queried one and the
    JPEGs stay. Neither half is an erasure.
    """
    urls(index_url="http://127.0.0.1:8013", api_url="https://search.example.com")
    assert sc._erase_targets() == [
        "http://127.0.0.1:8013", "https://search.example.com",
    ]


# ── result mapping ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_index_is_success_with_nothing_to_erase(urls):
    """An empty list, and `all([])` is True — a VMS with no index still fulfils.

    Smart Search is optional. If this returned a failure instead, every erasure
    on a deployment without an index would be stuck in_progress forever.
    """
    urls()
    assert await sc.erase_range("cam1", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z") == []


@pytest.mark.asyncio
async def test_clean_erase_reports_ok_with_counts(urls, monkeypatch):
    urls(index_url="http://idx:8013", api_url="http://idx:8013")
    calls = fake_client(monkeypatch, lambda url: FakeResponse(200, {
        "status": "ok", "persons_deleted": 12, "vehicles_deleted": 3,
        "crops_unlinked": 15, "crops_failed": 0,
    }))
    out = await sc.erase_range("cam1", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
    assert [x["ok"] for x in out] == [True]
    assert out[0]["persons_deleted"] == 12 and out[0]["crops_unlinked"] == 15
    assert calls[0]["url"] == "http://idx:8013/erase"
    assert calls[0]["params"] == {"camera": "cam1", "from": "2026-01-01T00:00:00Z",
                                  "to": "2026-01-02T00:00:00Z"}


@pytest.mark.asyncio
async def test_rows_gone_but_crops_left_on_disk_is_a_failure(urls, monkeypatch):
    """The case that looks erased everywhere and is not.

    A 200 is not enough. The index deleted its rows — so nothing searches, and
    every count in the product reads zero — while JPEGs of the data subject are
    still sitting in the crop directory.
    """
    urls(index_url="http://idx:8013")
    fake_client(monkeypatch, lambda url: FakeResponse(200, {
        "status": "incomplete", "persons_deleted": 9, "vehicles_deleted": 1,
        "crops_unlinked": 6, "crops_failed": 4,
    }))
    out = await sc.erase_range("cam1", "a", "b")
    assert out[0]["ok"] is False
    assert "4 crop file(s) remain on disk" in out[0]["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 405])
async def test_index_without_an_erasure_endpoint_fails_loudly(urls, monkeypatch, status):
    """An older index, or the retiring clip-service. It cannot be erased.

    Reported as a failure rather than skipped: a searchable copy of the subject
    exists that this VMS cannot destroy, and that has to reach the operator.
    """
    urls(api_url="https://legacy.example.com")
    fake_client(monkeypatch, lambda url: FakeResponse(status))
    out = await sc.erase_range("cam1", "a", "b")
    assert out[0]["ok"] is False
    assert "no erasure endpoint" in out[0]["error"]


@pytest.mark.asyncio
async def test_unreachable_index_is_never_reported_as_erased(urls, monkeypatch):
    urls(index_url="http://idx:8013")

    def boom(url):
        raise httpx.ConnectError("connection refused")

    fake_client(monkeypatch, boom)
    out = await sc.erase_range("cam1", "a", "b")
    assert out[0]["ok"] is False
    assert "unreachable" in out[0]["error"]


@pytest.mark.asyncio
async def test_one_failing_index_does_not_mask_the_other(urls, monkeypatch):
    """Both are attempted, and the aggregate the caller computes is False.

    A short-circuit on first failure would leave the second index un-erased with
    nothing recorded about it.
    """
    urls(index_url="http://good:8013", api_url="http://bad:8013")
    fake_client(monkeypatch, lambda url: (
        FakeResponse(200, {"status": "ok"}) if "good" in url else FakeResponse(500)
    ))
    out = await sc.erase_range("cam1", "a", "b")
    assert len(out) == 2
    assert [x["ok"] for x in out] == [True, False]
    assert not all(x["ok"] for x in out)   # what dsr.py gates fulfilment on
