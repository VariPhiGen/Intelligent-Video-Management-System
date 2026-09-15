"""Shared fixtures — chiefly the first in-process HTTP client this backend has had.

Every test in this service until now called functions directly. That covers the
logic well and leaves a whole layer unasserted: status codes, request
validation, response shapes, and above all whether a route is reachable by
somebody who should not reach it. Authorisation here is a dependency on a route
or a mount, so it only exists once a REQUEST is routed — calling the handler
function bypasses the very thing that protects it.

So the fixtures below drive the real app over ASGI, in-process, with no server
and no network:

  app     the real `backend.main.app`, with `get_db` overridden and the policy
          cache pre-seeded, so nothing reaches Postgres or Valkey.
  client  httpx.AsyncClient over ASGITransport.
  token   mints a REAL RS256 JWT that `security._decode_bearer` verifies.

The token fixture is deliberately not a stub of `get_principal`. A test that
patches out authentication cannot tell you that authentication works. Instead a
throwaway RSA key is generated once, `security._jwks` is pointed at its public
half, and everything downstream — signature verification, `exp`, `iat`, the
issuer allow-list, `_roles_from_claims`, `require_role`, `require_capability` —
runs exactly as it does in production. That is what makes "this token is
expired" and "this token is signed by the wrong key" testable at all.

No lifespan is run. `ASGITransport` does not emit startup events, which is the
point: `main.lifespan` opens the database, Valkey and the Keycloak admin client.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── A throwaway signing key, generated once per session ────────────────────

@pytest.fixture(scope="session")
def rsa_key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    # 2048 is the smallest size PyJWT will accept for RS256 without warning.
    # Generated per session rather than checked in: a private key in the tree,
    # even a test one, is a thing that gets copied somewhere it matters.
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def _issuer():
    from backend.config import settings

    # The real allow-list, so a token minted here is accepted for the same
    # reason a Keycloak token is. Hard-coding an issuer would test nothing.
    return sorted(settings.oidc_allowed_issuers)[0]


@pytest.fixture
def mint(rsa_key, _issuer, monkeypatch):
    """Mint a signed bearer token. `mint(roles=["admin"])` -> Authorization dict.

    Every argument has a default that produces a VALID token, so a test states
    only the thing it is varying — an expiry in the past, a foreign issuer, a
    missing claim — and the rest stays realistic.
    """
    import jwt as pyjwt

    from backend import security

    # Point the module's JWKS client at our public key. `_decode_bearer` still
    # does the real jwt.decode; only where the key comes from changes.
    public = rsa_key.public_key()
    monkeypatch.setattr(
        security, "_jwks",
        lambda: SimpleNamespace(
            get_signing_key_from_jwt=lambda token: SimpleNamespace(key=public)
        ),
    )
    # A stale client from an earlier test must not survive into this one.
    monkeypatch.setattr(security, "_jwk_client", None, raising=False)

    def _mint(
        roles=(),
        *,
        subject="tester",
        issuer=None,
        expires_in=3600,
        issued_at=None,
        session_id="sess-1",
        client_roles=None,
        key=None,
        extra_claims=None,
        omit=(),
    ):
        now = int(time.time())
        claims = {
            "iss": issuer if issuer is not None else _issuer,
            "sub": subject,
            "preferred_username": subject,
            "iat": issued_at if issued_at is not None else now,
            "exp": now + expires_in,
            "sid": session_id,
            "realm_access": {"roles": list(roles)},
        }
        if client_roles:
            claims["resource_access"] = {
                c: {"roles": list(r)} for c, r in client_roles.items()
            }
        if extra_claims:
            claims.update(extra_claims)
        for name in omit:
            claims.pop(name, None)
        token = pyjwt.encode(claims, key or rsa_key, algorithm="RS256")
        return {"Authorization": f"Bearer {token}"}

    return _mint


# ── The app, with every external service cut off ───────────────────────────

class StubSession:
    """Enough AsyncSession to let a handler run without a database.

    Reads come back empty and writes are counted. This is a CONTRACT fixture:
    it exists so a request can reach a handler and produce a status code and a
    body shape. Tests that care what the database actually did belong in the
    direct-call suites, which is where they already are.
    """

    def __init__(self):
        self.commits = 0
        self.added = []

    async def execute(self, statement=None, *a, **kw):
        # The create path takes a PostgreSQL advisory lock before inserting and
        # 409s if it cannot get one. With a blanket falsy scalar() every create
        # returns "Concurrent slug reservation conflict", which looks like a
        # passing validation test and is nothing of the kind — it is the reason
        # this stub answers this one statement specifically.
        if "pg_try_advisory_xact_lock" in str(statement):
            return SimpleNamespace(scalar=lambda: 1, scalar_one_or_none=lambda: None)
        return SimpleNamespace(
            scalar_one_or_none=lambda: None,
            scalar=lambda: 0,
            scalars=lambda: SimpleNamespace(all=lambda: [], first=lambda: None),
            all=lambda: [],
            first=lambda: None,
            fetchall=lambda: [],
            mappings=lambda: SimpleNamespace(all=lambda: []),
        )

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None

    async def refresh(self, obj, *a, **kw):
        """Fill in what the database would have supplied on insert.

        Server-side defaults (the primary key, the timestamps, the starting
        health) are None on a freshly constructed ORM object and only appear
        after a real flush. Without them the response model cannot validate and
        a create returns 500 — so a test asserting the created SHAPE needs
        these, and only these.
        """
        import uuid as _uuid
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        for field, value in (
            ("id", _uuid.uuid4()),
            ("created_at", now),
            ("updated_at", now),
            ("health_status", "unknown"),
            ("motion_detection", False),
            ("search_indexing", False),
        ):
            if getattr(obj, field, None) is None:
                setattr(obj, field, value)
        return None

    async def flush(self):
        return None

    async def delete(self, obj):
        return None

    def add(self, obj):
        self.added.append(obj)

    def begin(self):
        """`async with db.begin():` — the delete path opens a transaction."""
        class _Tx:
            async def __aenter__(_s):
                return _s

            async def __aexit__(_s, *a):
                return False

        return _Tx()

    async def close(self):
        return None


class _StubResponse:
    """An httpx-shaped response that always says "nothing here, and that's fine"."""

    status_code = 200
    text = "{}"
    headers: dict[str, str] = {}

    def json(self):
        return {}

    def raise_for_status(self):
        return None


class _StubHTTP:
    """Stands in for an httpx.AsyncClient held by a service module."""

    def __init__(self):
        self.calls = []

    async def _record(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return _StubResponse()

    async def get(self, url, **kw):
        return await self._record("GET", url, **kw)

    async def post(self, url, **kw):
        return await self._record("POST", url, **kw)

    async def put(self, url, **kw):
        return await self._record("PUT", url, **kw)

    async def patch(self, url, **kw):
        return await self._record("PATCH", url, **kw)

    async def delete(self, url, **kw):
        return await self._record("DELETE", url, **kw)

    async def request(self, method, url, **kw):
        return await self._record(method, url, **kw)

    async def aclose(self):
        return None


class StubRedis:
    """An in-memory stand-in for the Valkey client.

    Not a general Redis: only the commands this backend actually issues
    (`grep -rhoE 'await r\\.[a-z_]+\\(' backend/`), which are few and simple.
    Real enough that a handler reading back what it wrote sees it, which is
    what the job-state paths in scan_manager need.

    It exists because P0 found the same failure one layer down: a test that
    stubs HTTP but not the broker opens a socket to 127.0.0.1:6380 and passes
    only on a developer's machine with the stack up.
    """

    def __init__(self):
        self.store: dict[str, object] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None, nx=False, **kw):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def setex(self, key, seconds, value):
        self.store[key] = value
        return True

    async def delete(self, *keys):
        n = 0
        for key in keys:
            n += self.store.pop(key, None) is not None
            n += self.hashes.pop(key, None) is not None
        return n

    async def exists(self, key):
        return int(key in self.store or key in self.hashes)

    async def expire(self, key, seconds):
        return True

    async def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hset(self, key, field=None, value=None, mapping=None):
        bucket = self.hashes.setdefault(key, {})
        if mapping:
            bucket.update({str(k): str(v) for k, v in mapping.items()})
        elif field is not None:
            bucket[str(field)] = str(value)
        return 1

    async def hincrby(self, key, field, amount=1):
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = str(int(bucket.get(field, 0)) + amount)
        return int(bucket[field])

    async def eval(self, script, numkeys, *args):
        return 1

    async def ping(self):
        return True

    async def aclose(self):
        return None


def _outbound_modules():
    """Service modules that hold an httpx client to another service."""
    from backend.services import (
        analytics_client,
        frames_client,
        motion_client,
        nvr_client,
        relay,
        smartsearch_client,
    )

    return [relay, nvr_client, motion_client, analytics_client,
            frames_client, smartsearch_client]


@pytest.fixture
def db():
    return StubSession()


@pytest.fixture
def app(db, monkeypatch):
    """The real app with its external edges stubbed.

    Anything this does NOT stub is a real code path under test — routing,
    dependency resolution, validation, serialisation and the whole of
    security.py.
    """
    from backend import main as main_module
    from backend.config import settings
    from backend.db import get_db
    from backend.services import audit, policy

    application = main_module.app

    async def _get_db():
        yield db

    application.dependency_overrides[get_db] = _get_db

    # Not every session comes from the injected dependency. The audit trail,
    # the policy read, the relay's re-register sweep and the substream
    # auto-resolve each open their OWN session with AsyncSessionLocal, so
    # overriding get_db alone leaves them dialling Postgres — which is exactly
    # what the network guard below caught on the first create that succeeded.
    #
    # audit.py binds the name at import time; the rest import it inside the
    # function, so both the module attribute and the source need replacing.
    class _SessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return db

        async def __aexit__(self, *a):
            return False

    factory = _SessionFactory()
    monkeypatch.setattr("backend.db.AsyncSessionLocal", factory)
    monkeypatch.setattr(audit, "AsyncSessionLocal", factory, raising=False)

    # The dev bypass hands every caller all five roles. It must be off, or every
    # authorisation test below would pass for the wrong reason.
    monkeypatch.setattr(settings, "dev_auth", False)
    monkeypatch.setattr(settings, "internal_api_key", "test-internal-key")

    # Seed the policy cache so require_capability resolves from DEFAULT_POLICY
    # instead of opening a connection to Postgres. get_policies() swallows a
    # failed read and falls back to defaults anyway, but it would spend a
    # connection timeout doing it on every capability-gated request.
    monkeypatch.setattr(policy, "_cache", policy._merged({}))
    monkeypatch.setattr(policy, "_cache_at", time.monotonic())

    # Downstream services. Left live, GET /api/cameras really does try to reach
    # MediaMTX and only survives because the handler logs the failure and moves
    # on — a test that passes because a connection was REFUSED fast enough is
    # not hermetic, and would behave differently on a machine where something
    # happens to be listening on that port.
    #
    # Patched at the client factory, the same seam the direct-call suites use,
    # so the service modules' own logic still runs against a stub transport.
    for module in _outbound_modules():
        monkeypatch.setattr(module, "_get_client", lambda: _StubHTTP(), raising=False)

    # Valkey. Patched at get_redis so every caller — redis_client's own helpers,
    # audit, health, scan_manager — shares one in-memory store per test.
    from backend import redis_client

    fake = StubRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr(redis_client, "get_redis", _get_redis)
    monkeypatch.setattr(redis_client, "_redis", fake, raising=False)
    for name in ("audit", "health", "scan_manager"):
        module = __import__(f"backend.services.{name}", fromlist=[name])
        if hasattr(module, "get_redis"):
            monkeypatch.setattr(module, "get_redis", _get_redis)

    # Fail closed. Everything above is a stub that SHOULD mean no test in this
    # file opens a connection — but "should" is how the P0 failure got in, and
    # a stub that is quietly bypassed leaves a test passing on a developer's
    # machine and failing on a runner. So make the absence of network an
    # assertion rather than an expectation.
    #
    # Hooked at the event loop's create_connection: that is the one path
    # asyncpg, redis-py's asyncio client and httpx all end up in, and unlike
    # patching socket.connect it cannot disturb the loop's own self-pipe.
    import asyncio

    real_create_connection = asyncio.base_events.BaseEventLoop.create_connection

    async def _refuse(self, protocol_factory, host=None, port=None, **kw):
        raise AssertionError(
            f"a test opened a connection to {host}:{port}. These are contract "
            f"tests and must not reach a real service — add a stub in "
            f"conftest.py rather than starting the stack."
        )

    monkeypatch.setattr(
        asyncio.base_events.BaseEventLoop, "create_connection", _refuse
    )

    try:
        yield application
    finally:
        application.dependency_overrides.clear()
        asyncio.base_events.BaseEventLoop.create_connection = real_create_connection


@pytest_asyncio.fixture
async def client(app):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c


@pytest.fixture
def camera_id():
    """A syntactically valid camera id, so a 404/403 is about the CAMERA and
    not about the path failing to parse."""
    return str(uuid.uuid4())
