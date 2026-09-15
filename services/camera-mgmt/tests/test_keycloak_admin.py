"""keycloak_admin.py — the product's identity plane, 372 lines, no test.

The core half of the identity plane: the admin token, the request wrapper, error
mapping and the self-service password check. Its failures are the kind that
lock people out of a running site rather than the kind that raise. Two areas
carry the risk.

(Role assignment — `set_roles`, and the account-breaking composite bug its tests
pin — moved to the identity extension on 2026-09-15, and its tests moved with it
into test_identity_extension.py. That extension is not part of the open core.)

THE PASSWORD CHECK. Keycloak answers `invalid_grant` for BOTH "wrong password"
and "this account has a pending required action". Reporting the second as the
first sends a user hunting for a password that was never wrong, and each guess
counts toward a lockout.

THE TOKEN RETRY. A stale admin token must be retried once. Until 2026-09-10 it
retried without bound, so a 401 that would never clear became ~1000 requests to
Keycloak and a RecursionError. Fixed here; pinned below.

Hermetic: `_get_client` is replaced, so no Keycloak and no network.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services import keycloak_admin as kc  # noqa: E402


def response(status=200, body=None, text=""):
    def _json():
        if body is None:
            raise ValueError("no json")
        return body
    return SimpleNamespace(status_code=status, json=_json, text=text or str(body or ""))


class FakeKeycloak:
    """Records every call and answers from a script."""

    def __init__(self, token_status=200, request_answers=None, post_answers=None):
        self.token_calls = 0
        self.requests: list[tuple] = []
        self.posts: list[tuple] = []
        self._token_status = token_status
        self._request_answers = list(request_answers or [])
        self._post_answers = list(post_answers or [])

    async def post(self, url, **kw):
        # The admin token endpoint and the pwcheck grant both arrive here.
        if "realms/master" in url:
            self.token_calls += 1
            if self._token_status != 200:
                return response(self._token_status, text="nope")
            return response(200, {"access_token": f"tok-{self.token_calls}",
                                  "expires_in": 300})
        self.posts.append((url, kw))
        return self._post_answers.pop(0) if self._post_answers else response(200, {})

    async def request(self, method, url, **kw):
        self.requests.append((method, url, kw))
        if self._request_answers:
            return self._request_answers.pop(0)
        return response(200, [])


@pytest.fixture(autouse=True)
def reset_token_cache(monkeypatch):
    """The module caches the admin token in a global; leaking it across tests
    would make the token-retry tests depend on ordering."""
    monkeypatch.setattr(kc, "_token", None, raising=False)
    monkeypatch.setattr(kc, "_token_exp", 0, raising=False)
    yield


def use(monkeypatch, fake):
    monkeypatch.setattr(kc, "_get_client", lambda: fake)
    return fake


# ── The admin token ────────────────────────────────────────────────────────

class TestAdminToken:
    @pytest.mark.asyncio
    async def test_a_token_is_fetched_and_reused(self, monkeypatch):
        fake = use(monkeypatch, FakeKeycloak())
        assert await kc._admin_token() == "tok-1"
        assert await kc._admin_token() == "tok-1"
        assert fake.token_calls == 1, "the token was re-fetched while still valid"

    @pytest.mark.asyncio
    async def test_the_cache_expires_early_rather_than_late(self, monkeypatch):
        # Cached until 30s BEFORE expiry: a token that expires in flight is a
        # 401 storm, and the margin is what avoids it.
        fake = use(monkeypatch, FakeKeycloak())
        await kc._admin_token()
        import time as _t
        assert kc._token_exp <= _t.monotonic() + 300 - 30 + 1

    @pytest.mark.asyncio
    async def test_a_short_lived_token_still_gets_a_sane_floor(self, monkeypatch):
        # max(30, expires_in - 30): a 10-second token must not produce a
        # negative window that re-fetches on every single call.
        class ShortLived(FakeKeycloak):
            async def post(self, url, **kw):
                if "realms/master" in url:
                    self.token_calls += 1
                    return response(200, {"access_token": "t", "expires_in": 10})
                return response(200, {})

        use(monkeypatch, ShortLived())
        import time as _t
        before = _t.monotonic()
        await kc._admin_token()
        assert kc._token_exp >= before + 30

    @pytest.mark.asyncio
    async def test_a_rejected_admin_login_is_502_not_a_crash(self, monkeypatch):
        use(monkeypatch, FakeKeycloak(token_status=401))
        with pytest.raises(HTTPException) as exc:
            await kc._admin_token()
        assert exc.value.status_code == 502

    @pytest.mark.asyncio
    async def test_an_unreachable_keycloak_is_502(self, monkeypatch):
        class Down(FakeKeycloak):
            async def post(self, url, **kw):
                raise httpx.ConnectError("no route to host")

        use(monkeypatch, Down())
        with pytest.raises(HTTPException) as exc:
            await kc._admin_token()
        assert exc.value.status_code == 502
        assert "unreachable" in exc.value.detail


class TestTokenRetry:
    @pytest.mark.asyncio
    async def test_a_stale_token_is_retried_once_with_a_fresh_one(self, monkeypatch):
        fake = use(monkeypatch, FakeKeycloak(request_answers=[
            response(401), response(200, [{"name": "admin"}]),
        ]))
        resp = await kc._req("GET", "/roles")
        assert resp.status_code == 200
        assert fake.token_calls == 2, "the retry reused the stale token"
        assert len(fake.requests) == 2

    @pytest.mark.asyncio
    async def test_a_permanent_401_stops_after_one_retry(self, monkeypatch):
        # THE REGRESSION. An admin that has lost its realm-management role
        # answers 401 forever; the unbounded version issued ~1000 requests and
        # then raised RecursionError, surfacing as a 500 explaining nothing.
        fake = use(monkeypatch, FakeKeycloak(request_answers=[
            response(401, text="a"), response(401, text="b"),
            response(401, text="c"), response(401, text="d"),
        ]))
        resp = await kc._req("GET", "/roles")
        assert resp.status_code == 401, "the 401 must be returned, not retried away"
        assert len(fake.requests) == 2, (
            f"expected one attempt plus one retry, got {len(fake.requests)}"
        )

    @pytest.mark.asyncio
    async def test_a_persistent_401_reaches_the_caller_as_a_reportable_error(self, monkeypatch):
        use(monkeypatch, FakeKeycloak(request_answers=[response(401), response(401)]))
        with pytest.raises(HTTPException) as exc:
            kc._raise_for(await kc._req("GET", "/roles"), "Could not list roles")
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_a_non_401_failure_is_not_retried(self, monkeypatch):
        fake = use(monkeypatch, FakeKeycloak(request_answers=[response(500)]))
        resp = await kc._req("GET", "/roles")
        assert resp.status_code == 500
        assert len(fake.requests) == 1


class TestErrorMapping:
    def test_a_client_error_becomes_a_400(self):
        with pytest.raises(HTTPException) as exc:
            kc._raise_for(response(409, {"errorMessage": "User exists with same username"}),
                          "Could not create user")
        assert exc.value.status_code == 400
        assert "User exists" in exc.value.detail

    def test_a_server_error_becomes_a_502(self):
        with pytest.raises(HTTPException) as exc:
            kc._raise_for(response(503, {"error": "upstream"}), "Could not list users")
        assert exc.value.status_code == 502

    def test_a_body_that_is_not_json_still_produces_a_readable_error(self):
        with pytest.raises(HTTPException) as exc:
            kc._raise_for(response(500, None, text="<html>502 Bad Gateway</html>"),
                          "Could not list users")
        assert exc.value.detail == "Could not list users"

    def test_a_success_raises_nothing(self):
        assert kc._raise_for(response(204), "Could not delete user") is None


# ── The password check, and the ambiguity it has to resolve ────────────────

class TestVerifyPassword:
    def _pwcheck(self, monkeypatch, answer):
        monkeypatch.setattr(kc, "_get_pwcheck_secret", _secret)
        return use(monkeypatch, FakeKeycloak(post_answers=[answer]))

    @pytest.mark.asyncio
    async def test_the_right_password_verifies(self, monkeypatch):
        self._pwcheck(monkeypatch, response(200, {"access_token": "x"}))
        assert await kc.verify_password("ana", "correct") is True

    @pytest.mark.asyncio
    async def test_the_wrong_password_is_a_plain_false(self, monkeypatch):
        self._pwcheck(monkeypatch, response(
            401, {"error": "invalid_grant",
                  "error_description": "Invalid user credentials"}))
        assert await kc.verify_password("ana", "wrong") is False

    @pytest.mark.asyncio
    async def test_an_incomplete_account_is_not_reported_as_a_bad_password(self, monkeypatch):
        # THE AMBIGUITY. Keycloak answers invalid_grant for both. Reporting
        # this one as "wrong password" sends the user guessing — and each guess
        # counts toward a lockout they did not earn.
        self._pwcheck(monkeypatch, response(
            400, {"error": "invalid_grant",
                  "error_description": "Account is not fully set up"}))
        with pytest.raises(HTTPException) as exc:
            await kc.verify_password("ana", "correct")
        assert exc.value.status_code == 409
        assert "required action" in exc.value.detail

    @pytest.mark.asyncio
    async def test_an_unexpected_keycloak_answer_is_502_not_a_false(self, monkeypatch):
        # False would mean "your password is wrong", which is a claim this
        # module has no evidence for when Keycloak answers something else.
        self._pwcheck(monkeypatch, response(500, {"error": "server_error"}))
        with pytest.raises(HTTPException) as exc:
            await kc.verify_password("ana", "whatever")
        assert exc.value.status_code == 502

    @pytest.mark.asyncio
    async def test_a_non_json_error_body_is_502(self, monkeypatch):
        self._pwcheck(monkeypatch, response(502, None, text="<html>gateway</html>"))
        with pytest.raises(HTTPException) as exc:
            await kc.verify_password("ana", "whatever")
        assert exc.value.status_code == 502

    @pytest.mark.asyncio
    async def test_an_unreachable_keycloak_is_502(self, monkeypatch):
        monkeypatch.setattr(kc, "_get_pwcheck_secret", _secret)

        class Down(FakeKeycloak):
            async def post(self, url, **kw):
                raise httpx.ConnectError("down")

        use(monkeypatch, Down())
        with pytest.raises(HTTPException) as exc:
            await kc.verify_password("ana", "x")
        assert exc.value.status_code == 502

    @pytest.mark.asyncio
    async def test_the_check_uses_the_confidential_client_not_the_spa_one(self, monkeypatch):
        # vms-web is public; enabling password grants there would let anyone on
        # the network trade a username and password for a token.
        fake = self._pwcheck(monkeypatch, response(200, {"access_token": "x"}))
        await kc.verify_password("ana", "correct")
        data = fake.posts[0][1]["data"]
        assert data["client_id"] == kc.PWCHECK_CLIENT_ID
        assert data["client_id"] != "vms-web"
        assert data["client_secret"] == "shh"


async def _secret():
    return "shh"
