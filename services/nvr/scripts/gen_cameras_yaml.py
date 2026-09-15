#!/usr/bin/env python3
"""Generate config/cameras.yaml for a specific server from CAMERA_INVENTORY.md.

Usage:
    python3 scripts/gen_cameras_yaml.py --server s110|s100|s139|s113

Parses the markdown inventory (single source of truth) and writes a server-specific
cameras.yaml with normalized RTSP URL encoding (literal @ -> %40 in credentials,
literal & -> %26 in query strings).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def normalize_rtsp_url(url: str) -> str:
    """Ensure literal @ in credentials become %40 and literal & in query become %26.

    The host portion of an RTSP URL never contains @, so the rightmost @ is always
    the creds/host separator. Already-encoded %40 / %26 are left untouched.
    """
    m = re.match(r"^(rtsps?://)(.+)$", url)
    if not m:
        return url
    scheme, rest = m.group(1), m.group(2)
    last_at = rest.rfind("@")
    if last_at < 0:
        return url
    creds = rest[:last_at]
    tail = rest[last_at:]  # includes the separator @

    creds = creds.replace("@", "%40")

    if "?" in tail:
        path, query = tail.split("?", 1)
        query = query.replace("&", "%26")
        tail = f"{path}?{query}"

    return f"{scheme}{creds}{tail}"


def parse_inventory(text: str, server: str) -> list[tuple[str, str]]:
    """Return [(camera_name, rtsp_url), ...] for the given server section.

    Section headers are `## Server <name> (<ip>)` — but s110's heading is mistyped
    as `## Servee s110`, so we accept either spelling.
    """
    # Build a regex that finds the server section and captures everything until
    # the next ## heading.
    pattern = re.compile(
        rf"^##\s+(?:Server|Servee)\s+{re.escape(server)}\s*\([^)]*\)\s*$(.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(text)
    if not m:
        return []

    section = m.group(1)
    cameras: list[tuple[str, str]] = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        # Split into cells (drop leading/trailing empty from outer pipes)
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 3:
            continue
        sensor_id, name, url = cells
        # Skip header and separator rows
        if sensor_id.lower() == "sensor id" or set(sensor_id) <= set("-: "):
            continue
        if not url.startswith("rtsp"):
            continue
        cameras.append((name, normalize_rtsp_url(url)))
    return cameras


def yaml_quote(s: str) -> str:
    """Quote a string for YAML using double quotes; escape embedded quotes."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_yaml(cameras: list[tuple[str, str]], retention_days: int = 2) -> str:
    out: list[str] = ["cameras:"]
    for name, url in cameras:
        out.append(f"  - name: {yaml_quote(name)}")
        out.append(f"    rtsp_url: {yaml_quote(url)}")
        out.append(f"    retention_days: {retention_days}")
        out.append("")
    out.append("recording:")
    out.append("  segment_duration: 60")
    out.append("  storage_path: /data/nvr")
    out.append("  clips_path: /data/nvr/clips")
    out.append("  clip_ttl_minutes: 30")
    out.append("")
    out.append("ffmpeg:")
    out.append("  rtsp_transport: tcp")
    out.append("  reconnect_delay: 5")
    out.append("  max_reconnect_attempts: 0")
    out.append("  loglevel: warning")
    out.append("")
    return "\n".join(out)


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True, choices=["s110", "s100", "s139", "s113"])
    ap.add_argument("--inventory", default=str(repo_root / "CAMERA_INVENTORY.md"))
    ap.add_argument("--output", default=str(repo_root / "config" / "cameras.yaml"))
    ap.add_argument("--retention-days", type=int, default=2)
    args = ap.parse_args()

    inv_path = Path(args.inventory)
    if not inv_path.exists():
        print(f"error: inventory not found: {inv_path}", file=sys.stderr)
        return 1

    cameras = parse_inventory(inv_path.read_text(), args.server)
    if not cameras:
        print(f"error: no cameras found for server '{args.server}' in {inv_path}", file=sys.stderr)
        return 1

    yaml_text = render_yaml(cameras, args.retention_days)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml_text)
    print(f"Wrote {len(cameras)} cameras for {args.server} -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
