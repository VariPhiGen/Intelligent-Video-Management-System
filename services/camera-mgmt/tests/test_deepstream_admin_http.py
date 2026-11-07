"""Administration → DeepStream over HTTP: who may reach it, what is forwarded
to the pipeline, and how its answers come back.

The pipeline itself is replaced by an httpx MockTransport, so these tests see
exactly the request the VMS would send to :8081 — key header, multipart fields
and all — without a pipeline (or a restart of one) anywhere.
"""
from __future__ import annotations

import functools

import httpx
import pytest

from backend.services import deepstream_client as ds

URL = "/api/deepstream/models"


@pytest.fixture
def pipeline(monkeypatch):
    """Route the client's calls to a handler; returns the captured requests."""
    seen: list[httpx.Request] = []
    answers: dict[tuple[str, str], httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        request.read()
        return answers.get((request.method, request.url.path),
                           httpx.Response(404, json={"error": "not_found"}))

    real = httpx.AsyncClient
    monkeypatch.setattr(ds.httpx, "AsyncClient",
                        functools.partial(real, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(ds.settings, "internal_api_key", "k-test")
    monkeypatch.setattr(ds, "_url_override", None)
    return seen, answers


def _upload(name="school_v2", onnx_name="m.onnx", restart="false"):
    return dict(
        data={"name": name, "batch_size": "1", "network_size": "640", "restart": restart},
        files={"onnx": (onnx_name, b"\x08\x01onnx", "application/octet-stream"),
               "labels": ("labels.txt", b"person\ncar\n", "text/plain")},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "operator", "supervisor", "dpo"])
async def test_only_an_admin_may_touch_the_pipeline(client, mint, pipeline, role):
    seen, _ = pipeline
    assert (await client.get(URL, headers=mint([role]))).status_code == 403
    assert (await client.post(URL, headers=mint([role]), **_upload())).status_code == 403
    assert (await client.post(f"{URL}/restart", headers=mint([role]))).status_code == 403
    assert seen == []   # refused before anything reached DeepStream


@pytest.mark.asyncio
async def test_list_relays_the_pipeline_answer_with_the_key(client, mint, pipeline):
    seen, answers = pipeline
    answers[("GET", "/model")] = httpx.Response(200, json={"models": [{"name": "school_v2"}]})
    r = await client.get(URL, headers=mint(["admin"]))
    assert r.status_code == 200
    assert r.json() == {"models": [{"name": "school_v2"}]}
    assert seen[0].headers["X-Internal-Key"] == "k-test"


@pytest.mark.asyncio
async def test_upload_forwards_every_field(client, mint, pipeline):
    seen, answers = pipeline
    answers[("POST", "/model/add")] = httpx.Response(200, json={"ok": True})
    r = await client.post(URL, headers=mint(["admin"]), **_upload(restart="true"))
    assert r.status_code == 200, r.text
    body = seen[0].content
    assert seen[0].headers["X-Internal-Key"] == "k-test"
    for part in (b'name="name"\r\n\r\nschool_v2', b'name="batch_size"\r\n\r\n1',
                 b'name="network_size"\r\n\r\n640', b'name="restart"\r\n\r\ntrue',
                 b'name="onnx"; filename="m.onnx"', b'name="labels"; filename="labels.txt"',
                 b"person\ncar\n"):
        assert part in body


@pytest.mark.asyncio
async def test_a_blank_network_size_is_left_to_the_pipeline(client, mint, pipeline):
    """The pipeline's own curl omits network_size; so must we when it is blank."""
    seen, answers = pipeline
    answers[("POST", "/model/add")] = httpx.Response(200, json={"ok": True})
    up = _upload()
    del up["data"]["network_size"]
    r = await client.post(URL, headers=mint(["admin"]), **up)
    assert r.status_code == 200, r.text
    assert b'name="network_size"' not in seen[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [dict(name="bad name"), dict(name="../x"),
                                    dict(onnx_name="m.pt")])
async def test_bad_uploads_never_reach_the_pipeline(client, mint, pipeline, kwargs):
    seen, _ = pipeline
    r = await client.post(URL, headers=mint(["admin"]), **_upload(**kwargs))
    assert r.status_code == 422
    assert seen == []


@pytest.mark.asyncio
async def test_a_pipeline_refusal_reaches_the_screen(client, mint, pipeline):
    _, answers = pipeline
    answers[("POST", "/model/add")] = httpx.Response(409, json={"error": "model_exists"})
    r = await client.post(URL, headers=mint(["admin"]), **_upload())
    assert r.status_code == 409
    assert "model_exists" in r.json()["detail"]


@pytest.mark.asyncio
async def test_restart_posts_to_the_pipeline(client, mint, pipeline):
    seen, answers = pipeline
    answers[("POST", "/model/restart")] = httpx.Response(200, json={"restart_scheduled": True})
    r = await client.post(f"{URL}/restart", headers=mint(["admin"]))
    assert r.status_code == 200
    assert [(q.method, q.url.path) for q in seen] == [("POST", "/model/restart")]


@pytest.mark.asyncio
async def test_a_down_pipeline_is_a_502_not_a_crash(client, mint, monkeypatch):
    def down(request):
        raise httpx.ConnectError("refused", request=request)
    real = httpx.AsyncClient
    monkeypatch.setattr(ds.httpx, "AsyncClient",
                        functools.partial(real, transport=httpx.MockTransport(down)))
    r = await client.get(URL, headers=mint(["admin"]))
    assert r.status_code == 502
    assert "unreachable" in r.json()["detail"]


# ── Where the pipeline is ────────────────────────────────────────────────────

SETTINGS = "/api/deepstream/settings"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "operator", "supervisor", "dpo"])
async def test_only_an_admin_may_see_or_move_the_pipeline_address(client, mint, pipeline, role):
    assert (await client.get(SETTINGS, headers=mint([role]))).status_code == 403
    assert (await client.put(SETTINGS, json={"api_url": "http://10.0.0.5:8081"},
                             headers=mint([role]))).status_code == 403
    assert (await client.post(f"{SETTINGS}/test", json={"api_url": "http://10.0.0.5:8081"},
                              headers=mint([role]))).status_code == 403


@pytest.mark.asyncio
async def test_without_a_saved_address_the_environment_default_is_used(client, mint, pipeline):
    r = await client.get(SETTINGS, headers=mint(["admin"]))
    assert r.status_code == 200
    body = r.json()
    assert body["api_url"] is None
    assert body["effective_url"] == body["default_url"] == ds.settings.deepstream_api_url.rstrip("/")


@pytest.mark.asyncio
async def test_a_saved_address_is_where_calls_go(client, mint, pipeline):
    seen, answers = pipeline
    answers[("GET", "/model")] = httpx.Response(200, json={"models": []})
    r = await client.put(SETTINGS, json={"api_url": " http://10.0.0.5:9090/ "}, headers=mint(["admin"]))
    assert r.status_code == 200, r.text
    assert r.json()["api_url"] == r.json()["effective_url"] == "http://10.0.0.5:9090"
    assert (await client.get(URL, headers=mint(["admin"]))).status_code == 200
    assert str(seen[-1].url) == "http://10.0.0.5:9090/model"


@pytest.mark.asyncio
async def test_clearing_the_address_goes_back_to_the_default(client, mint, pipeline):
    await client.put(SETTINGS, json={"api_url": "http://10.0.0.5:9090"}, headers=mint(["admin"]))
    r = await client.put(SETTINGS, json={"api_url": ""}, headers=mint(["admin"]))
    assert r.status_code == 200
    assert r.json()["api_url"] is None
    assert r.json()["effective_url"] == r.json()["default_url"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["10.0.0.5:8081", "ftp://10.0.0.5", "http://", "http://h:99999",
                                 "http://u:p@10.0.0.5:8081", "http://10.0.0.5:8081/?x=1"])
async def test_an_address_that_is_not_a_plain_http_url_is_refused(client, mint, pipeline, url):
    r = await client.put(SETTINGS, json={"api_url": url}, headers=mint(["admin"]))
    assert r.status_code == 422
    assert ds._base() == ds.default_url()   # nothing applied


@pytest.mark.asyncio
async def test_testing_an_address_probes_it_without_saving(client, mint, pipeline):
    seen, answers = pipeline
    answers[("GET", "/health")] = httpx.Response(200, json={"status": "ok"})
    r = await client.post(f"{SETTINGS}/test", json={"api_url": "http://10.0.0.5:9090"},
                          headers=mint(["admin"]))
    assert r.status_code == 200, r.text
    assert r.json()["health"] == {"status": "ok"}
    assert str(seen[-1].url) == "http://10.0.0.5:9090/health"
    assert ds._base() == ds.default_url()


@pytest.mark.asyncio
async def test_testing_a_dead_address_says_so(client, mint, pipeline):
    r = await client.post(f"{SETTINGS}/test", json={"api_url": "http://10.0.0.5:9090"},
                          headers=mint(["admin"]))   # no /health answer registered → 404
    assert r.status_code == 502
    assert "10.0.0.5:9090" in r.json()["detail"]
