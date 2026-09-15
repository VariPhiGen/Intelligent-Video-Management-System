"""Is every mounted router actually guarded?

Authorisation in this backend is DECLARED, not derived. A router is protected
because somebody wrote `dependencies=[Depends(require_authenticated)]` next to
its `include_router` call in main.py, or `dependencies=[...]` on its own
`APIRouter(...)`. Both are one line, both are easy to leave out, and leaving one
out is silent: the app starts, the routes work, and they work for anybody.

That is the same shape as the bug class test_sub_track_wiring.py exists for —
"tracks.py has been the single definition of a camera's tracks since the sub
shipped… the masks endpoint, the delete path, the purge proxy and the DSR
erasure all drifted anyway, each by simply not calling it." Nothing about
authorisation is different. There are 78 endpoints behind these mounts, and
whether they are reachable anonymously currently depends on eleven lines nobody
is forced to look at.

So these tests read main.py and the router modules STRUCTURALLY, with ast, and
assert on the mount table itself. They are deliberately about wiring: they do
not check that `require_role("admin")` is the *right* role for a route, only
that a route is not accidentally left with no gate at all. A new router mounted
without a dependency has to fail something here.

The four allowlisted routers are not taken at their word either — each carries
its own test below proving it still guards itself the way the allowlist claims.
An entry here that stops being true fails just as loudly as a missing one.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
MAIN = BACKEND / "main.py"
ROUTERS = BACKEND / "routers"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Callables that establish a principal. A dependency naming one of these is a
# gate; anything else (get_db, a rate limiter) is not, however many there are.
AUTH_DEPENDENCIES = frozenset({
    "require_authenticated",
    "require_role",
    "require_capability",
})

# Routers mounted WITHOUT an include-level auth dependency, and why. Adding a
# name here is a deliberate act: it needs a reason, and it needs the paired test
# below that proves the reason is still true.
UNGUARDED_AT_MOUNT = {
    "auth_router": "/auth/config is public by necessity; /me guards itself",
    "nvr_router": "single-login proxy; principal resolved per-request in the handler",
    "motion_router": "same proxy pattern as nvr",
    "hls_router": "live-preview relay, deliberately unauthenticated (mirrors Caddy /hls/*)",
}
# users_router left this table when user administration moved to the identity
# extension; test_identity_extension.py holds its guard test now.


# ── Reading the mount table ────────────────────────────────────────────────

def _dependency_names(node: ast.AST) -> set[str]:
    """Every callable named anywhere inside a `dependencies=[...]` list.

    Handles both `Depends(require_authenticated)` and the called form
    `Depends(require_capability("camera_manage"))`, because the difference is
    only whether the factory takes an argument.
    """
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _mount_table() -> dict[str, set[str]]:
    """{router variable -> auth dependency names on its include_router call}."""
    tree = ast.parse(MAIN.read_text())
    mounts: dict[str, set[str]] = {}
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        fn = call.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "include_router"):
            continue
        if not call.args or not isinstance(call.args[0], ast.Name):
            continue
        name = call.args[0].id
        deps: set[str] = set()
        for kw in call.keywords:
            if kw.arg == "dependencies":
                deps = _dependency_names(kw.value)
        mounts[name] = deps & AUTH_DEPENDENCIES
    return mounts


def _router_module(router_var: str) -> Path:
    """`cameras_router` -> backend/routers/cameras.py, via main.py's imports."""
    tree = ast.parse(MAIN.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.asname == router_var:
                    return BACKEND.parent / (node.module.replace(".", "/") + ".py")
    raise AssertionError(f"no import in main.py binds {router_var}")


def _apirouter_dependencies(module: Path) -> set[str]:
    """Auth dependencies declared on the module's own `APIRouter(...)`."""
    tree = ast.parse(module.read_text())
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) \
                and call.func.id == "APIRouter":
            for kw in call.keywords:
                if kw.arg == "dependencies":
                    return _dependency_names(kw.value) & AUTH_DEPENDENCIES
    return set()


def _route_decorators(module: Path) -> list[ast.Call]:
    """Every `@router.get/post/.../api_route(...)` decorator in a module."""
    tree = ast.parse(module.read_text())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) \
                    and isinstance(dec.func.value, ast.Name) \
                    and dec.func.value.id == "router":
                out.append(dec)
    return out


def _route_functions(module: Path) -> list[ast.AST]:
    tree = ast.parse(module.read_text())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
               and isinstance(d.func.value, ast.Name) and d.func.value.id == "router"
               for d in node.decorator_list):
            out.append(node)
    return out


MOUNTS = _mount_table()


# ── The invariant ──────────────────────────────────────────────────────────

def test_main_actually_mounts_routers():
    """Guard the guard: if the parse silently found nothing, every test below
    would pass by vacuum."""
    assert len(MOUNTS) >= 11, f"expected the full mount table, parsed {MOUNTS}"


@pytest.mark.parametrize("router_var", sorted(MOUNTS))
def test_every_mounted_router_is_guarded_or_declared(router_var):
    """A router reaches main.py with an auth dependency, or it is on the
    allowlist with a reason. There is no third option."""
    if router_var in UNGUARDED_AT_MOUNT:
        pytest.skip(f"allowlisted: {UNGUARDED_AT_MOUNT[router_var]}")
    assert MOUNTS[router_var], (
        f"{router_var} is mounted with no authentication dependency. Add "
        f"dependencies=[Depends(require_authenticated)] to its include_router "
        f"call, or add it to UNGUARDED_AT_MOUNT with the reason and a test "
        f"proving it guards itself."
    )


def test_the_allowlist_names_only_routers_that_exist():
    """An allowlist entry for a router that has been renamed or removed is a
    hole waiting for the name to be reused."""
    unknown = set(UNGUARDED_AT_MOUNT) - set(MOUNTS)
    assert not unknown, f"allowlist names routers that are not mounted: {unknown}"


# ── Each allowlisted router must still earn its place ──────────────────────

@pytest.mark.parametrize("router_var", ["nvr_router", "motion_router"])
def test_the_proxy_routers_resolve_a_principal_in_every_route(router_var):
    """The allowlist says these enforce auth "inside the proxy". They each have
    a single catch-all route, and that route must actually do it — a proxy that
    forgets forwards anonymously to a service that trusts it."""
    module = _router_module(router_var)
    functions = _route_functions(module)
    assert functions, f"{module.name} declares no routes; the allowlist is stale"
    for fn in functions:
        called = {n.func.id for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "get_principal" in called, (
            f"{module.name}:{fn.name} is a route on a router mounted without an "
            f"auth dependency and does not call get_principal()"
        )


def test_the_hls_router_is_still_only_the_preview_relay():
    """hls_router is the one deliberately anonymous mount. That is defensible
    for a streamed live preview mirroring the Caddy handler; it stops being
    defensible the moment a second route joins it, so adding one must fail
    here and force the decision back into the open."""
    decorators = _route_decorators(_router_module("hls_router"))
    assert len(decorators) == 1, (
        f"routers/hls.py now declares {len(decorators)} routes. This router is "
        f"mounted with NO authentication; a new route on it is anonymous."
    )


def test_the_public_auth_routes_are_exactly_the_ones_we_expect():
    """/auth/config must stay public (the SPA reads it before login) and the
    rest of auth.py must not join it by accident."""
    module = _router_module("auth_router")
    public = set()
    for fn in _route_functions(module):
        signature_names = {
            n.id for a in fn.args.defaults + fn.args.kw_defaults if a
            for n in ast.walk(a) if isinstance(n, ast.Name)
        }
        if not (signature_names & AUTH_DEPENDENCIES):
            for dec in fn.decorator_list:
                if isinstance(dec, ast.Call) and dec.args and \
                        isinstance(dec.args[0], ast.Constant):
                    public.add(dec.args[0].value)
    assert public == {"/auth/config"}, (
        f"unauthenticated auth routes are {public or '{}'}; expected only "
        f"/auth/config. A new public route here is reachable by anyone."
    )
