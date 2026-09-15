#!/usr/bin/env python3
"""import_discovery_db.py — one-shot import of the retired discovery database.

The discovery microservice kept its staging rows (scanned devices, encrypted
credentials, ONVIF metadata) in a separate `rtsp_discovery` database. After the
merge (migration 004) that data lives in the unified `cameras` table. This
script copies it over once:

  • a discovered_devices row whose IP matches a REGISTERED camera enriches that
    camera in place (vendor/model/firmware, ports, candidates, encrypted creds)
  • any other row becomes a staging camera row at the equivalent stage
    ('added' rows with no matching camera fall back to 'verified')

Skipping this script is fine — a rescan repopulates staging rows; registered
cameras are unaffected either way. Credentials stay decryptable as long as
DISCOVERY_SECRET_KEY is unchanged. Idempotent: IPs already present in cameras
as staging rows are skipped. Run it BEFORE dropping the rtsp_discovery DB:

  python3 import_discovery_db.py \
      --relay     postgresql://rtsp:secret@127.0.0.1:5433/rtsp_relay \
      --discovery postgresql://rtsp:secret@127.0.0.1:5433/rtsp_discovery
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import asyncpg

_STAGE_MAP = {"added": "verified"}  # stage for unmatched 'added' devices


async def run(relay_dsn: str, discovery_dsn: str) -> None:
    relay = await asyncpg.connect(relay_dsn)
    disco = await asyncpg.connect(discovery_dsn)
    enriched = created = skipped = 0
    try:
        devices = await disco.fetch("SELECT * FROM discovered_devices")
        print(f"found {len(devices)} device rows in rtsp_discovery")

        for d in devices:
            candidates = d["rtsp_candidates"] or "[]"
            open_ports = d["open_ports"] or "[]"
            if not isinstance(candidates, str):
                candidates = json.dumps(candidates)
            if not isinstance(open_ports, str):
                open_ports = json.dumps(open_ports)

            registered = await relay.fetchrow(
                "SELECT id FROM cameras WHERE ip = $1 AND stage = 'registered' LIMIT 1",
                d["ip"],
            )
            if registered is not None:
                await relay.execute(
                    """UPDATE cameras SET
                         mac = COALESCE(mac, $2), onvif_port = COALESCE(onvif_port, $3),
                         rtsp_port = COALESCE(rtsp_port, $4), vendor = COALESCE(vendor, $5),
                         model = COALESCE(model, $6), firmware = COALESCE(firmware, $7),
                         rtsp_candidates = $8::jsonb, open_ports = $9::jsonb,
                         enc_username = COALESCE(enc_username, $10),
                         enc_password = COALESCE(enc_password, $11),
                         last_scanned_at = $12
                       WHERE id = $1""",
                    registered["id"], d["mac"], d["onvif_port"], d["rtsp_port"],
                    d["vendor"], d["model"], d["firmware"], candidates, open_ports,
                    d["enc_username"], d["enc_password"], d["last_scanned_at"],
                )
                enriched += 1
                continue

            staging = await relay.fetchval(
                "SELECT 1 FROM cameras WHERE ip = $1 LIMIT 1", d["ip"]
            )
            if staging is not None:
                skipped += 1
                continue

            stage = _STAGE_MAP.get(d["status"], d["status"])
            await relay.execute(
                """INSERT INTO cameras
                     (id, stage, enabled, recording, ip, mac, onvif_port, rtsp_port,
                      vendor, model, firmware, rtsp_candidates, open_ports,
                      enc_username, enc_password, discovery_error, last_scanned_at)
                   VALUES ($1, $2, false, true, $3, $4, $5, $6, $7, $8, $9,
                           $10::jsonb, $11::jsonb, $12, $13, $14, $15)""",
                d["id"], stage, d["ip"], d["mac"], d["onvif_port"], d["rtsp_port"],
                d["vendor"], d["model"], d["firmware"], candidates, open_ports,
                d["enc_username"], d["enc_password"], d["error"], d["last_scanned_at"],
            )
            created += 1

        print(f"done: {enriched} registered cameras enriched, "
              f"{created} staging rows created, {skipped} skipped (ip already present)")
    finally:
        await relay.close()
        await disco.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--relay", required=True, help="rtsp_relay DSN (postgresql://…)")
    ap.add_argument("--discovery", required=True, help="rtsp_discovery DSN (postgresql://…)")
    args = ap.parse_args()
    try:
        asyncio.run(run(args.relay, args.discovery))
    except asyncpg.PostgresError as exc:
        sys.exit(f"database error: {exc}")
