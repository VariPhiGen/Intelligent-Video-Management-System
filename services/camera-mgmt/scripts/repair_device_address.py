#!/usr/bin/env python3
"""repair_device_address.py — point ONVIF back at the cameras, not the relay.

`cameras.ip` is the DEVICE address: every ONVIF call (maintenance, imaging, OSD,
encoder, PTZ) dials it, and the scanner dedupes on it. Until this was fixed it
was filled from the RTSP URL's host, which is the camera only when nothing
re-publishes the stream. Register cameras by a relay's re-published URLs —
`rtsp://<relay>:8554/<slug>`, as a bulk CSV of MediaMTX paths does — and every
row carries the RELAY's address, so all device settings fail with a connection
refused against a host nobody typed. The relay speaks RTSP and no ONVIF.

This repairs those rows in place. For each camera whose stored address is not
its own, it derives the real one, PROVES it by asking the device
(GetDeviceInformation over the candidate ONVIF ports), and only then writes:

  • ip            the proven device address
  • onvif_port    the port that answered
  • vendor/model/firmware   as reported by the device
  • credentials   Fernet-encrypted, when --store-credentials is given (the
                  common case here: these rows were bulk-imported and have none,
                  and one address without credentials still unlocks nothing)

Where the real address comes from, best source first:

  --from-relay   the re-publishing MediaMTX's own config API. Its `source` per
                 path IS the camera's URL — host, and often credentials too —
                 so this recovers addresses no local field records, matched to
                 cameras by the path in their RTSP URL. The authoritative source
                 when the stream is relayed, because that box is the one still
                 talking to the camera.
  --map          a `slug,ip` CSV, for anything the other two cannot supply.
  the name/slug  where a bulk-imported camera's IP usually survives
                 ("… 192.168.2.155", "…-192-168-2-155-k2ly").

Nothing is written for a candidate the device does not answer on, so a bad guess
stays a report line rather than a new wrong address. Dry-run unless --apply.
Idempotent: a camera already verified on the right address is skipped.

  # see what it would do (no writes)
  docker run --rm --network host -v "$PWD/services/camera-mgmt":/app -w /app \\
      -e DATABASE_URL -e DISCOVERY_SECRET_KEY --entrypoint python variphi-vms-api \\
      scripts/repair_device_address.py --from-relay http://192.168.2.240:9997

  # write the proven ones, storing the credentials that proved them
  … scripts/repair_device_address.py --from-relay http://192.168.2.240:9997 \\
      --store-credentials --apply
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import ipaddress
import os
import re
import sys
from pathlib import Path

from urllib.parse import unquote, urlparse

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# A dotted quad in a camera's NAME, and the same flattened into a slug. The slug
# form is deliberately anchored to hyphens on both sides so a stray run of digits
# in a name ("2nd FLR COMP LAB-1") cannot be read as an address.
_IP_IN_NAME = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
_IP_IN_SLUG = re.compile(r"(?:^|-)(\d{1,3})-(\d{1,3})-(\d{1,3})-(\d{1,3})(?:-|$)")


def _valid(ip: str) -> str | None:
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        return None


def candidate_address(name: str, slug: str) -> str | None:
    """The camera's real address as recorded in its own name or slug, if at all."""
    for m in _IP_IN_NAME.finditer(name or ""):
        if (ip := _valid(m.group(1))):
            return ip
    if (m := _IP_IN_SLUG.search(slug or "")):
        if (ip := _valid(".".join(m.groups()))):
            return ip
    return None


def from_rtsp_url(url: str | None) -> tuple[str, str | None, str | None] | None:
    """`(host, user, password)` out of a camera's OWN RTSP URL.

    Worth reading for a camera registered by its direct URL: that URL is the one
    still pulling the stream, so its host is the device and its credentials are
    live ones. Ranked below the name/slug for a RELAYED row, where the host is
    the re-publisher — the very address being repaired.
    """
    if not url:
        return None
    u = urlparse(url)
    if not u.hostname:
        return None
    return (u.hostname,
            unquote(u.username) if u.username else None,
            unquote(u.password) if u.password else None)


def read_relay(base: str) -> dict[str, tuple[str, str | None, str | None]]:
    """`{relay path: (camera host, user, password)}` from a MediaMTX config API.

    A relayed camera's real address only exists in the relay's `source` URL for
    that path — the registry copied the relay's own host instead. The credentials
    in that URL are the ones still pulling the stream, so they are the ones worth
    verifying with when a site's cameras do not share one password.
    """
    import httpx

    out: dict[str, tuple[str, str | None, str | None]] = {}
    url = f"{base.rstrip('/')}/v3/config/paths/list"
    with httpx.Client(timeout=15) as http:
        page, pages = 0, 1
        while page < pages:
            r = http.get(url, params={"itemsPerPage": 500, "page": page})
            r.raise_for_status()
            body = r.json()
            pages = max(1, int(body.get("pageCount", 1)))
            for item in body.get("items") or []:
                src = item.get("source") or ""
                if not src.startswith(("rtsp://", "rtsps://")):
                    continue  # a publisher-fed or non-RTSP path knows no camera
                u = urlparse(src)
                if not u.hostname:
                    continue
                out[item["name"]] = (
                    u.hostname,
                    unquote(u.username) if u.username else None,
                    unquote(u.password) if u.password else None,
                )
            page += 1
    return out


def load_map(path: str) -> dict[str, str]:
    """`slug,ip` CSV (header optional) for cameras that never carried their IP."""
    out: dict[str, str] = {}
    with open(path, newline="") as fh:
        for row in csv.reader(fh):
            if len(row) < 2:
                continue
            slug, ip = row[0].strip(), row[1].strip()
            if not slug or slug.lower() == "slug":
                continue
            if not _valid(ip):
                raise SystemExit(f"--map: {ip!r} (slug {slug}) is not an IP address")
            out[slug] = _valid(ip)
    return out


async def run(dsn: str, user: str | None, password: str | None, ports: list[int],
              overrides: dict[str, str], relay: dict[str, tuple[str, str | None, str | None]],
              store_creds: bool, apply: bool, only: set[str], concurrency: int) -> int:
    from backend.crypto import encrypt
    from backend.services import onvif_probe

    conn = await asyncpg.connect(dsn)
    rows = await conn.fetch(
        """SELECT id, slug, name, ip, onvif_port, rtsp_url,
                  enc_password IS NOT NULL AS has_creds
             FROM cameras
            WHERE stage = 'registered'
         ORDER BY name"""
    )

    todo, skipped, unknown = [], [], []
    for r in rows:
        if only and r["slug"] not in only:
            continue
        # Best source first. `own` is the camera's own URL, which is the device
        # only when nothing re-publishes the stream — so its HOST ranks below the
        # name/slug, while its credentials are as good as the relay's.
        own = from_rtsp_url(r["rtsp_url"])
        via = relay.get(urlparse(r["rtsp_url"] or "").path.lstrip("/"))
        want = (overrides.get(r["slug"])
                or (via[0] if via else None)
                or candidate_address(r["name"], r["slug"])
                or (own[0] if own and not via else None))
        # Credentials: this camera's own, else the ones on the command line. A
        # camera with neither cannot be verified, so it is not touched.
        src = via or own or (None, None, None)
        creds = (src[1] or user, src[2] if src[2] is not None else password)
        if want is None or not creds[0] or creds[1] is None:
            if not (r["ip"] and r["onvif_port"] and r["has_creds"]):
                unknown.append(r)      # nothing to derive it from — say so, don't guess
            continue
        if r["ip"] == want and r["onvif_port"] and r["has_creds"]:
            skipped.append(r)          # already verified on the right address
            continue
        todo.append((r, want, creds))

    print(f"{len(rows)} registered cameras: {len(todo)} to check, "
          f"{len(skipped)} already verified, {len(unknown)} with no known address")

    sem = asyncio.Semaphore(concurrency)
    results: list[tuple] = []

    async def check(row, want, creds):
        async with sem:
            res = await onvif_probe.probe(want, ports, creds[0], creds[1])
            results.append((row, want, res, creds))

    await asyncio.gather(*(check(*t) for t in todo))
    results.sort(key=lambda t: t[0]["name"] or "")

    proven = [t for t in results if t[2].get("status") == "ok"]
    failed = [t for t in results if t[2].get("status") != "ok"]

    for row, want, res, _ in results:
        mark = "ok  " if res.get("status") == "ok" else "FAIL"
        detail = (f"port {res['onvif_port']} · {res.get('vendor')} {res.get('model')}"
                  if res.get("status") == "ok"
                  else f"{res.get('status')}: {res.get('error')}")
        print(f"  {mark} {row['name'][:44]:44} {row['ip'] or '-':>15} → {want:<15} {detail}")

    if not apply:
        print(f"\ndry run — {len(proven)} would be repaired, {len(failed)} did not answer. "
              "Re-run with --apply to write.")
    else:
        async with conn.transaction():
            for row, want, res, creds in proven:
                # The credentials that just answered — per camera, because the
                # relay may hold a different one for each.
                enc_user = encrypt(creds[0]) if store_creds else None
                enc_pass = encrypt(creds[1]) if store_creds else None
                await conn.execute(
                    """UPDATE cameras SET
                         ip = $2, onvif_port = $3,
                         vendor   = COALESCE($4, vendor),
                         model    = COALESCE($5, model),
                         firmware = COALESCE($6, firmware),
                         enc_username = COALESCE($7, enc_username),
                         enc_password = COALESCE($8, enc_password)
                       WHERE id = $1""",
                    row["id"], want, res["onvif_port"],
                    res.get("vendor"), res.get("model"), res.get("firmware"),
                    enc_user, enc_pass,
                )
        print(f"\nrepaired {len(proven)} cameras"
              f"{' (credentials stored)' if store_creds else ''}; "
              f"{len(failed)} left alone — they did not answer ONVIF at the address derived "
              "for them, so nothing was overwritten.")

    for row in unknown:
        print(f"  ??   {row['name'][:44]:44} {row['ip'] or '-':>15} "
              "→ no address in its name or slug; add it to --map")

    await conn.close()
    return 0 if not failed else 1


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"),
                    help="Postgres DSN (default: $DATABASE_URL)")
    ap.add_argument("--username", help="ONVIF username, when --from-relay has none")
    ap.add_argument("--password", help="ONVIF password, when --from-relay has none")
    ap.add_argument("--from-relay", metavar="URL",
                    help="MediaMTX config API of the box re-publishing these streams, "
                         "e.g. http://192.168.2.240:9997 — its per-path source URL is "
                         "where a relayed camera's real address and credentials live")
    ap.add_argument("--ports", default="80,8000,8080,2020,8899",
                    help="ONVIF ports to try, in order (default: %(default)s)")
    ap.add_argument("--map", help="CSV of slug,ip for cameras whose name has no IP")
    ap.add_argument("--only", action="append", default=[], metavar="SLUG",
                    help="Repair just this camera (repeatable) — use it for a first pass")
    ap.add_argument("--store-credentials", action="store_true",
                    help="Also store the verifying credentials (encrypted) on repaired rows")
    ap.add_argument("--concurrency", type=int, default=8,
                    help="Cameras probed at once (default: %(default)s)")
    ap.add_argument("--apply", action="store_true", help="Write. Without it, dry run.")
    args = ap.parse_args()

    if not args.database_url:
        raise SystemExit("no database URL — pass --database-url or set DATABASE_URL")
    # asyncpg speaks plain postgresql://; the app's DSN carries the SQLAlchemy driver.
    dsn = args.database_url.replace("+asyncpg", "")
    ports = [int(p) for p in args.ports.split(",") if p.strip()]
    overrides = load_map(args.map) if args.map else {}
    relay = read_relay(args.from_relay) if args.from_relay else {}
    if relay:
        print(f"{len(relay)} camera addresses read from {args.from_relay}")
    if not relay and not (args.username and args.password is not None):
        raise SystemExit("nothing to verify with — pass --username/--password, "
                         "or --from-relay to take them from the relay")

    sys.exit(asyncio.run(run(
        dsn, args.username, args.password, ports, overrides, relay,
        args.store_credentials, args.apply, set(args.only), args.concurrency,
    )))


if __name__ == "__main__":
    main()
