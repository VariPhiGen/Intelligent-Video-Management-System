"""Defaults written in two places must say the same thing.

A shipped default often lives twice: in a service's own config file, and as the
`${VAR:-default}` fallback in docker-compose.yml. The environment wins over the
file, so on an appliance the COMPOSE value is the one that runs, while the
config file is the one a reader trusts. Nothing else ties the two together, and
a change to one (a89fff5 moved the frame rate from 2 to 5 FPS in three places)
leaves the other silently describing a different product.

pyyaml only, like test_compose_topology.py: no docker needed.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SERVICES = yaml.safe_load(
    (ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]


def compose_default(service: str, var: str) -> str:
    """The fallback in `VAR: ${VAR:-default}` for one service."""
    raw = str(SERVICES[service]["environment"][var])
    m = re.fullmatch(r"\$\{" + re.escape(var) + r":-([^}]*)\}", raw)
    assert m, f"{service}.{var} is {raw!r}, not ${{{var}:-<default>}}"
    return m.group(1)


def env_example() -> dict[str, str]:
    """`VAR=value` lines from .env.example, ignoring comments."""
    out = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def test_the_frames_sample_rate_default_matches_its_config_file():
    """services/frames/tests/test_config.py pins config.yaml to CaptureConfig;
    this pins compose to config.yaml, which closes the loop."""
    shipped = yaml.safe_load(
        (ROOT / "services/frames/config.yaml").read_text(encoding="utf-8")
    )["capture"]["sample_fps"]
    assert float(compose_default("frames", "FRAMES_SAMPLE_FPS")) == float(shipped)


def test_smart_search_expects_the_rate_the_broker_actually_runs_at():
    """Smart Search does not sample: SEARCH_SAMPLE_FPS is only the rate its
    /health reports as `expected_source_fps`, and that field exists to make a
    real mismatch visible. It stayed at 2 when frames moved to 5, so the one
    check meant to reveal a mismatch was reporting one that did not exist."""
    assert (float(compose_default("smartsearch", "SEARCH_SAMPLE_FPS"))
            == float(compose_default("frames", "FRAMES_SAMPLE_FPS")))


def test_the_analytics_nested_containment_default_matches_its_config_file():
    """The documented no-rebuild off switch only works if compose passes the
    variable through at all — it did not, so ANALYTICS_NESTED_CONTAINMENT=0 in
    .env reached nothing. Reading it here is what proves it is passed."""
    shipped = yaml.safe_load(
        (ROOT / "services/analytics/config.yaml").read_text(encoding="utf-8")
    )["detect"]["nested_containment"]
    assert (float(compose_default("analytics", "ANALYTICS_NESTED_CONTAINMENT"))
            == float(shipped))


def test_the_sample_env_file_agrees_with_the_compose_defaults():
    """.env.example is what a first install copies to .env, and an explicit
    value there WINS over the compose fallback — so a stale line here quietly
    ships a different product than the one docker-compose.yml describes."""
    sample = env_example()
    for service, var in (("frames", "FRAMES_SAMPLE_FPS"),
                         ("smartsearch", "SEARCH_SAMPLE_FPS")):
        if var in sample:
            assert float(sample[var]) == float(compose_default(service, var)), var
