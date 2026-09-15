"""The bridge overlay must cover every host-networked service.

The base compose file uses `network_mode: host`, which Linux needs for ONVIF
WS-Discovery multicast and zero-NAT RTSP. On Docker Desktop "host" is a hidden
Linux VM the browser cannot reach, so docker-compose.bridge.yml moves every one
of those services onto the vms_internal bridge.

"Every one" is the load-bearing word, and it was not true: `smartsearch` shipped
with `network_mode: host` and no overlay entry, so on macOS and Windows the api
could not reach it, and it could not resolve the `mediamtx` name it is handed to
index from. Its own comment in the base file says "Same posture as motion" —
motion and nvr both got bridge treatment and it did not.

Nothing could have caught that, because a service is added to the base file and
the overlay is a separate file nobody is forced to open. This test is that
force. It reads both files structurally, so it fails on the NEXT service added
with host networking and no overlay entry, which is the actual failure mode —
not on this one, which is already fixed.

Run (from the repo root): python -m pytest tests -q
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "docker-compose.yml"
BRIDGE = ROOT / "docker-compose.bridge.yml"

# Services allowed to stay host-networked under the overlay, with the reason.
# Adding a name here is a deliberate act that should come with a line in the
# overlay's "Known limits vs Linux" header.
HOST_NETWORK_EXEMPT = {
    # The TLS front door is host-network and not remapped; TLS deployments are
    # Linux-first, and the overlay header says so.
    "caddy",
}


class _TolerantLoader(yaml.SafeLoader):
    """SafeLoader that survives compose's own tags (`!reset`, `!override`)."""


_TolerantLoader.add_multi_constructor(
    "!", lambda loader, suffix, node: None
)


def _load(path: Path) -> dict:
    with path.open() as fh:
        return yaml.load(fh, Loader=_TolerantLoader) or {}


BASE_SERVICES = _load(BASE).get("services", {})
BRIDGE_SERVICES = _load(BRIDGE).get("services", {})

HOST_NETWORKED = sorted(
    name for name, svc in BASE_SERVICES.items()
    if (svc or {}).get("network_mode") == "host"
)


def test_the_base_file_still_uses_host_networking():
    """Guards the premise. If this ever empties out, the overlay is obsolete and
    the rest of this file is asserting nothing."""
    assert HOST_NETWORKED, "no host-networked services — has the base file changed?"


@pytest.mark.parametrize("name", HOST_NETWORKED)
def test_every_host_networked_service_is_remapped(name):
    """The one that would have caught smartsearch."""
    if name in HOST_NETWORK_EXEMPT:
        pytest.skip(f"{name} is a documented exemption")
    assert name in BRIDGE_SERVICES, (
        f"{name!r} uses network_mode: host in docker-compose.yml but has no entry "
        f"in docker-compose.bridge.yml. On Docker Desktop it binds inside the VM: "
        f"unreachable from the other containers and from the browser, and unable "
        f"to resolve any service name. Add it to the overlay, or add it to "
        f"HOST_NETWORK_EXEMPT with a reason."
    )


@pytest.mark.parametrize("name", HOST_NETWORKED)
def test_a_remapped_service_joins_the_internal_network(name):
    """`!reset null` alone leaves a service on no network at all."""
    if name in HOST_NETWORK_EXEMPT or name not in BRIDGE_SERVICES:
        pytest.skip(f"{name} is not remapped")
    assert "vms_internal" in (BRIDGE_SERVICES[name].get("networks") or []), (
        f"{name!r} clears host networking but never joins vms_internal"
    )


def test_a_remapped_service_does_not_keep_a_loopback_bind():
    """Clearing network_mode is half the job: a service still binding 127.0.0.1
    inside its own netns is reachable by nothing. This is the other half of what
    was wrong with smartsearch."""
    offenders = []
    for name, svc in BRIDGE_SERVICES.items():
        for key, value in (svc.get("environment") or {}).items():
            if "BIND_HOST" in key and "127.0.0.1" in str(value):
                offenders.append(f"{name}.{key}={value}")
    assert not offenders, (
        "bridged services still binding loopback: " + ", ".join(offenders)
    )


def test_no_bridged_service_points_an_interconnect_at_loopback():
    """On the bridge, 127.0.0.1 is the container itself — an interconnect left
    on loopback silently reaches the wrong service. (MEDIAMTX_HLS_URL was
    exactly this: it resolved to the api container and every preview 404'd.)"""
    offenders = []
    for name, svc in BRIDGE_SERVICES.items():
        for key, value in (svc.get("environment") or {}).items():
            v = str(value)
            if ("127.0.0.1" in v or "localhost" in v) and "://" in v:
                offenders.append(f"{name}.{key}={v}")
    assert not offenders, (
        "bridged interconnects still on loopback: " + ", ".join(offenders)
    )


# ── The rendered article, when docker is available ─────────────────────────

docker_required = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker CLI not available"
)


def _render(profile: str | None = None) -> dict:
    cmd = ["docker", "compose", "-f", str(BASE), "-f", str(BRIDGE)]
    if profile:
        cmd += ["--profile", profile]
    cmd += ["config", "--format", "json"]
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        pytest.skip(f"compose config failed: {out.stderr[:200]}")
    return json.loads(out.stdout)


@docker_required
def test_the_rendered_overlay_leaves_nothing_on_host_networking():
    """The structural checks above read intent; this reads the result, including
    whatever compose's own merge semantics did with it."""
    rendered = _render(profile="search")
    left = sorted(n for n, s in rendered.get("services", {}).items()
                  if s.get("network_mode") == "host" and n not in HOST_NETWORK_EXEMPT)
    assert left == [], f"still host-networked after the overlay: {left}"


@docker_required
def test_smartsearch_is_reachable_and_points_at_the_searchdb_service():
    rendered = _render(profile="search")
    env = rendered["services"]["smartsearch"]["environment"]
    assert env["SMARTSEARCH_BIND_HOST"] == "0.0.0.0"
    assert "@searchdb:5432/" in env["SEARCHDB_URL"], (
        "the bridge must reach searchdb by service name, not by a published port "
        "that only happens to be bound inside the Desktop VM"
    )


@docker_required
def test_the_hls_proxy_points_at_the_mediamtx_service():
    """The fix this whole audit started from."""
    rendered = _render()
    assert rendered["services"]["api"]["environment"]["MEDIAMTX_HLS_URL"] \
        == "http://mediamtx:8988"


@docker_required
def test_the_linux_path_keeps_host_networking():
    """The base file alone is what a Linux appliance runs, and it must be
    untouched by any of this — the audited loopback posture depends on it."""
    out = subprocess.run(
        ["docker", "compose", "-f", str(BASE), "--profile", "search",
         "config", "--format", "json"],
        cwd=ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        pytest.skip("compose config failed")
    rendered = json.loads(out.stdout)
    assert rendered["services"]["smartsearch"]["network_mode"] == "host"
    assert rendered["services"]["api"]["network_mode"] == "host"
