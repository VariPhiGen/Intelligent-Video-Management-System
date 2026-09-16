# Variphi VMS

An on-premise video management system — a network video recorder for IP
cameras. Find the cameras on your network, pull each one exactly once, record
them continuously, and get clips and snapshots back out, behind one web UI and
one login.

It runs entirely on hardware you control: `./vms up -d` brings the whole stack
up in Docker on a single host, with no cloud account and no phone-home.

Licensed under **AGPL-3.0** — see [`LICENSE`](LICENSE), and [`NOTICE`](NOTICE)
for third-party attribution.

```
Browser ── Keycloak login (OIDC) ──▶  Variphi SPA + API  :8091 (API_PORT)
                                        │ /api/cameras       unified camera table (Postgres)
                                        │ /api/discovery/*   native ONVIF scan / probe / promote
                                        │ /api/nvr/*         → NVR service :8009 (127.0.0.1 only)
                                        ▼
   cameras ──single pull──▶ MediaMTX :8654 ──▶ rtsp://<SERVER_IP>:8654/<slug>
                                        │                      ▲
                                        └── NVR records from the relay ── consumers (DeepStream etc.)
```

---

## Features

**Legend:** ✅ shipped &nbsp;·&nbsp; 🚧 partly built &nbsp;·&nbsp; 🗓️ planned

### Cameras and recording

| | Feature |
|---|---|
| ✅ | ONVIF / WS-Discovery onboarding — network scan, probe, promote |
| ✅ | Manual and CSV camera entry; TCP sweep where multicast cannot reach |
| ✅ | Single-pull RTSP relay — one connection per camera serves any number of consumers |
| ✅ | Continuous recording, 60 s segments, codec-copy (no re-encode) |
| ✅ | Retention and per-camera storage caps, enforced by a pruning loop |
| ✅ | Sub-stream recording — record the low-bitrate track, view the high one |
| ✅ | ONVIF depth: encoder + Media2/H.265, imaging, OSD, reboot, clock sync |
| ✅ | Encrypted camera credentials, recoverable in the UI |
| ✅ | Live view (HLS), single- and multi-camera timeline playback |
| ✅ | Clip and snapshot export; HEVC→H.264 transcode at extraction time |
| 🚧 | H.265 live view — recording and playback work; the live tile cannot decode HEVC in-browser |
| 🗓️ | PTZ control — pan / tilt / zoom and presets over the ONVIF PTZ service |

### Detection and search

| | Feature |
|---|---|
| ✅ | Motion detection over the relay, opt-in per camera |
| ✅ | Frame broker — decode each camera once, share the pixels with every consumer |
| ✅ | Object detection (people, vehicles) on CPU via OpenVINO |
| ✅ | ANPR — plate detection and reading, open-baseline models |
| ✅ | Face detection and search-by-photo |
| ✅ | Smart Search — forensic search by description, plate or face |
| ✅ | AI activity events, Entry / Exit crossings, detections dashboard |
| 🚧 | GPU acceleration — available as a build arg, not yet selected automatically |
| 🗓️ | DeepStream GPU pipeline as a supported deployment shape |
| 🗓️ | Vision-language search — describe an event in plain language ("someone leaving a bag by the gate") and find it, including relationships CLIP cannot match |

### Platform

| | Feature |
|---|---|
| ✅ | Single login for the whole product — Keycloak OIDC; five roles, `viewer` (live only) through `admin` (everything) |
| ✅ | Capability-based authorization, enforced by the API and not only the UI |
| ✅ | Tamper-evident audit log |
| ✅ | Site maps — place cameras on a floor plan |
| ✅ | Health dashboard, per-camera uptime history |
| ✅ | Opt-in TLS front door (Caddy) |
| ✅ | Boot resilience for unattended appliances |
| ✅ | Peripheral inventory — barriers, relays, PLCs, UPS |
| 🗓️ | Peripherals rule engine — evaluate events against rules and raise alerts |
| 🗓️ | Outbound bridge — alerts to MQTT / Home Assistant, and on to field devices |

### Roadmap

The next block of work is the **peripherals event spine** — turning the
detections the analytics service already produces into actions a site can take.
The event spine (layer 1) shipped in September 2026. Layers 2 to 4 are designed
and awaiting implementation — nothing above layer 1 is wired up yet:

```
1. EVENT PRODUCTION   motion / smartsearch / analytics ──▶ analytics_events   ✅ shipped
2. RULE EVALUATION    analytics_events ──▶ alert_rules  ──▶ alerts            🗓️ designed
3. OUTBOUND BRIDGE    alerts ──▶ MQTT / Home Assistant  ──▶ device            🗓️ designed
4. FIELD LAYER        Home Assistant ──Modbus──▶ barrier / PLC / UPS          🗓️ designed
```

**PTZ control** is the other near-term addition: pan, tilt, zoom and presets
driven over the ONVIF PTZ service.

**Vision-language search** is the larger one, and the bigger change to how
search works. Smart Search today embeds crops with CLIP (`ViT-B-32`). CLIP
matches *appearance*: "man in a red jacket" works, because that is a property of
one object in one frame. What it cannot represent is a relationship or an event
— "someone leaving a bag by the gate" is two objects, an interaction, and a
change over time, and no amount of prompt wording recovers it from a single
appearance vector.

A vision-language model closes that by describing what is happening rather than
what is present: scene descriptions generated at index time become text the
index can search, so a plain-language query reaches events instead of objects.
The same descriptions explain *why* a clip matched, which is the difference
between a result list an operator trusts and one they re-verify by hand.

It is a substantially bigger change than swapping a checkpoint — heavier
inference, a second index alongside the vectors, and different hardware
expectations on an appliance that runs detection on CPU today. It is on this
list because it is the direction, not because it is close.

Also queued: automatic GPU selection for Smart Search embedding, in-browser
H.265 live view, and widening component and E2E test coverage beyond the four
representative suites described in [`TESTING.md`](TESTING.md).

Nothing here is a delivery commitment. If a date matters to you, ask on the
issue tracker rather than inferring one from this list.

---

## Architecture

Cameras live in ONE table across their whole lifecycle: a network scan inserts
rows at `stage='discovered'`, probing/verification advance them, and promotion
flips them to `stage='registered'` (relayed, health-checked, recorded). Rescan
dedupe falls out of the same table — a scanned IP that already belongs to a
registered camera is simply shown as "In relay".

### Services (`services/`)

| Dir | Service | Port | Role |
|-----|---------|------|------|
| `../frontend-react` | React SPA | — | The product-wide UI, built into the api image and served at :8091 |
| `camera-mgmt` | FastAPI | `API_PORT` (8091) | Camera registry + ONVIF discovery, NVR proxy, serves the SPA |
| `nvr` | FastAPI + ffmpeg | 8009 (localhost) | 60s MPEG-TS recording, clips, snapshots |
| `motion` | FastAPI + OpenCV | 8012 (localhost) | Per-camera motion detection over the relay, opt-in per camera |
| `frames` | FastAPI + OpenCV | 8014 (localhost) | Frame broker: decodes each camera once into shared memory, so motion and analytics see the same pixels (compose profile `frames`) |
| `analytics` | FastAPI + YOLO (OpenVINO/CPU) | 8015 (localhost) | Opt-in per-camera detection — people, vehicles, plates, faces — sent to the search index (profile `analytics`) |
| `smartsearch` | FastAPI + pgvector | 8013 (localhost) | Forensic search index: search by description, plate or face photo (profile `search`) — see `services/smartsearch/README.md` |
| `../deploy` | MediaMTX | 8654/8988/9987 | Single camera pull, RTSP+HLS fan-out (`deploy/mediamtx.yml`) |
| `../deploy` | Keycloak | 8085 | OIDC identity — the single login (`deploy/keycloak/`, realm `vms`) |
| — | Postgres / Valkey | 5433 / 6380 | Registry+IdP storage / stream health (Valkey speaks Redis' protocol; the service, `REDIS_URL` and the `redis://` scheme keep their names) |
| — | Postgres + pgvector (`searchdb`) | 5434 (localhost) | Search index storage for `smartsearch` (profile `search`) |

A file-by-file tour for contributors lives in [`CODE-MAP.md`](CODE-MAP.md).

---

## Quick start

```bash
./vms up -d
# UI:      http://localhost:8091    (user 'admin'; password printed on first run; port = API_PORT)
# Keycloak http://localhost:8085    (master admin printed on first run)
```

That is the whole install. **The sign-in password is generated, not
`admin`/`admin`** — first run prints it and writes it to `.env` as
`VMS_ADMIN_PASSWORD` (`grep VMS_ADMIN_PASSWORD .env` to read it back), and the
realm's shipped `admin`/`admin` is retired before you ever reach the login page.
See [`HARDENING.md` §1](HARDENING.md#1-sign-in-credentials).

On first run `./vms` creates `.env` from
`.env.example` and generates real secrets, because the API refuses to start on
the shipped placeholder values — a deliberate guard, so that a box where a
guessed `X-Internal-Key` is full admin cannot ship by accident.

Then set `SERVER_IP` and `DISCOVERY_DEFAULT_CIDR` in `.env` for your network and
`./vms up -d` again: the loopback default works for a local trial, but cameras on
a LAN need the real address, and ONVIF discovery needs your camera subnet.

`./vms` is a thin `docker compose` wrapper — all compose args pass through
(`./vms ps`, `./vms logs -f api`, …). Plain `docker compose up -d` still works
on native Linux; on Docker Desktop add `-f docker-compose.bridge.yml`. Note that
the bare `docker compose` path skips the boot-resilience check below — on a GPU
host, run that once yourself.

Before putting a box in front of real cameras, read
[`HARDENING.md`](HARDENING.md): it covers the credentials, secrets, network
exposure and TLS decisions that a production appliance needs, and it is honest
about the gaps that remain.

### macOS / Windows (Docker Desktop)

The base compose targets a **native-Linux host** and uses `network_mode: host`
(required for ONVIF WS-Discovery multicast and zero-NAT RTSP). Docker Desktop
runs containers inside a Linux VM, so host-mode listeners bind *inside the VM*
and are unreachable from your browser — the UI never loads and live/recording
stay dark. Use the **bridge overlay** (`docker-compose.bridge.yml`), which moves
those services onto an internal bridge and publishes the browser/consumer ports.

`./vms` is a Bash script and will not run in Windows `cmd`/PowerShell, so there
is a `vms.cmd` wrapper that does the same job. It is the whole install — from a
fresh clone:

```
vms up -d
```

On first run (or if `.env` still holds shipped placeholders) it creates `.env`,
generates the three real secrets the boot guard requires
(`INTERNAL_API_KEY` / `DISCOVERY_SECRET_KEY` / `KEYCLOAK_ADMIN_PASSWORD`), adds
the bridge overlay, then passes every argument through to `docker compose` —
`vms ps`, `vms logs -f api`, `vms down`, etc. Then browse
`http://localhost:8091` as `admin`. Note that `gen-secrets.ps1` does **not**
generate `VMS_ADMIN_PASSWORD`, so on Windows the seeded `admin`/`admin` is still
live at first login (Keycloak marks it temporary and forces a change); set the
key by hand in `.env` to retire it up front, as the Linux path does.
In PowerShell call the wrapper as `.\vms.cmd`.

**Generate secrets without starting** (optional — `vms` does this for you):

```
scripts\gen-secrets.cmd -Fresh
```

`-Fresh` also generates `DISCOVERY_SECRET_KEY` — safe only on a new box with no
cameras yet (it encrypts stored camera credentials); omit it to rotate just
`INTERNAL_API_KEY` and `KEYCLOAK_ADMIN_PASSWORD`.

**Prefer plain `docker compose`?** Pass the overlay each time
(`docker compose -f docker-compose.yml -f docker-compose.bridge.yml up -d`) or
set it once in `.env` — but **mind the path separator**, which differs by OS:

```
# macOS / Linux (colon):
COMPOSE_FILE=docker-compose.yml:docker-compose.bridge.yml
# Windows (semicolon):
COMPOSE_FILE=docker-compose.yml;docker-compose.bridge.yml
```

Going the plain-`docker compose` route means generating secrets yourself first
(the `gen-secrets` command above); only `vms` does that automatically.

The overlay also sets `MTX_RTSPADDRESS`/`MTX_APIADDRESS`/… so MediaMTX binds all
interfaces (the base file keeps it loopback on Linux), with a matching
full-access auth grant for the Docker bridge subnet (`172.16.0.0/12`) in
`deploy/mediamtx.yml`.

Docker Desktop limitations (both fine for local dev/eval, which is what this
overlay is for):

- **Camera auto-discovery** relies on WS-Discovery multicast, which cannot cross
  the VM NAT — use the TCP sweep or add cameras manually (both fully functional).
- Published-port traffic is SNAT'd, so MediaMTX sees LAN clients as the bridge
  gateway and **IP-based RTSP auth cannot distinguish LAN from internal**. For a
  real deployment run on **Linux with the base compose alone**, where source IPs
  are real and the loopback binds do the isolating.

### Boot resilience (run once per server)

```bash
sudo scripts/install-boot-resilience.sh --all     # --check first, if you prefer
```

Two one-time host changes. Neither restarts anything already running, and
`--uninstall` reverses both.

**Why it is not optional on a GPU host.** The NVIDIA toolkit writes its CDI spec
to `/var/run/cdi/` — tmpfs, wiped every boot — from a unit ordered
`After=multi-user.target`, and `docker.service` is `WantedBy` that same target.
The refresh therefore always runs *after* dockerd has already restarted
containers, which then fail with `CDI device injection failed: unresolvable CDI
devices nvidia.com/gpu=all`. Measured on the reference appliance: **5 boots, 5
failures, 0 recoveries.** The fix keeps the spec in `/etc/cdi/` (on disk, and one
of dockerd's two default spec dirs) using NVIDIA's own documented
`NVIDIA_CTK_CDI_OUTPUT_FILE_PATH`, so the race disappears rather than being won
more often.

**Why it also matters without a GPU.** Compose's `restart: unless-stopped`
restarts a container that *crashed*, but never retries one that **failed to
start** — and Docker only auto-starts containers that were running when the
daemon last stopped. So a single bad start, from any cause, leaves the appliance
down until a human intervenes. `vms.service` re-runs `up -d` after
`docker.service`, which closes that whole class. It deliberately overrides a
manual `docker stop`; use `systemctl stop vms` to take the stack down for real.

`./vms up` detects the CDI half and offers to install it, so an operator who
skips this section is prompted rather than surprised — but only on a host where
it applies, and only through `./vms`. Set `VMS_SKIP_PREFLIGHT=1` to silence it.

To log in from other machines, set in `.env`:
`OIDC_ISSUER=http://<SERVER_IP>:8085/realms/vms` and
`OIDC_PUBLIC_URL=http://<SERVER_IP>:8085`, then `docker compose up -d api`.

> Every port above is settable in `.env`. If something else on the host already
> holds one, change it there rather than stopping the stack halfway up.

---

## How recording follows the registry

Cameras added in the UI (or via Discovery) are stored in Postgres and pushed to
MediaMTX. The camera-mgmt API then **syncs them into the NVR automatically**:

- on create/enable → `POST /cameras/{slug}?rtsp_url=rtsp://<SERVER_IP>:8654/<slug>`
- on delete/disable → `DELETE /cameras/{slug}`
- every `NVR_SYNC_INTERVAL` (60s) a reconcile loop re-asserts the desired state —
  needed because NVR runtime cameras are in-memory (lost on NVR restart).

The NVR camera name **is the slug**, which is also the `nvr_camera_name` join
key used by downstream AI pipelines.

Recordings land in the `nvr_data` volume as `/data/nvr/<slug>/<slug>_YYYYMMDD_HHMMSS.ts`
(60 s segments, codec copy), indexed in SQLite, pruned per retention.

## Security model

- **Users**: Keycloak OIDC (realm `vms`). Five realm roles — `admin`,
  `supervisor`, `operator`, `viewer` and `dpo` — each with its own capability
  defaults in `services/policy.py`: `admin` holds every capability,
  `supervisor` adds camera and peripheral management to an operator's set, and
  `viewer` is live-only. The API validates Bearer JWTs via JWKS; mutating
  routes need `admin`.
  Further accounts are added in the Keycloak console, which is also the
  break-glass path for a locked-out administrator — see
  [`HARDENING.md` §1](HARDENING.md#1-sign-in-credentials).
- **Services**: discovery + NVR are reached only through the API's
  authenticated proxies; the shared `INTERNAL_API_KEY` covers service→service
  calls. The NVR itself binds to 127.0.0.1 (it has no auth of its own).
- **TLS** is opt-in for hardened sites: `./scripts/gen-certs.sh`
  then `docker compose --profile tls up -d caddy`.

[`HARDENING.md`](HARDENING.md) turns this into a checklist, and
[`SECURITY.md`](SECURITY.md) explains how to report a vulnerability.

## Operational notes

- `services/nvr/config/cameras.yaml` intentionally lists **no cameras** — the
  registry sync owns that. Manual entries there still work but won't be managed.
- `docker compose logs -f api nvr` is the first stop when a stream won't record:
  the api logs `nvr.sync.*` events; the nvr logs per-camera ffmpeg state.
- Postgres holds two DBs: `rtsp_relay` (unified camera table incl. discovery
  staging rows) and `keycloak`.
- The NVR clip API transcodes HEVC→H.264 at extraction time only; recording is
  always codec-copy.

---

## Documentation

| Document | What it covers |
|---|---|
| [`HARDENING.md`](HARDENING.md) | Taking an appliance to production, and the gaps that remain |
| [`CODE-MAP.md`](CODE-MAP.md) | File-by-file tour of the codebase |
| [`TESTING.md`](TESTING.md) | Test strategy, what is covered and what is not |
| [`RELEASE-NOTES.md`](RELEASE-NOTES.md) | What changed, and what an upgrade requires |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | How to propose a change |
| [`SECURITY.md`](SECURITY.md) | Reporting a vulnerability |
| [`NOTICE`](NOTICE) / [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md) | Attribution and licence obligations |

## Contributing

Issues and pull requests are welcome — start with
[`CONTRIBUTING.md`](CONTRIBUTING.md). Contributors sign a CLA ([`CLA.md`](CLA.md)),
which the bot will prompt for on your first pull request.

## Licence

AGPL-3.0. If you run a modified version over a network, §13 obliges you to offer
that version's source to its users — build with `VITE_SOURCE_URL` pointing at
your own repository so the UI's source link is honest.
