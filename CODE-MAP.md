# Code map — the essential files and what they contain

A guided tour for someone new to the codebase: where each piece of the product
lives, what the load-bearing files do, and how the services talk to each other.
Operational docs live in `README.md`.

## The product in one paragraph

A self-hosted VMS: cameras are discovered/registered by **camera-mgmt** (the
only service a browser talks to), pulled once each by the **MediaMTX** relay
and fanned out from there; the **NVR** records the relay streams to disk and
serves playback; **motion** analyses them for events; **frames** decodes each
stream once and shares the frames; **analytics** detects people, vehicles and
plates in them; **smartsearch** indexes the crops it finds for forensic search.
Identity is a
single **Keycloak** login; state lives in **Postgres** (registry) and
**Valkey** (ephemeral); a second Postgres (**searchdb**, pgvector) backs the
search index. Everything ships as one Docker Compose stack.

```
                    browser ── :8091 ──▶ camera-mgmt api (SPA + /api/*)
                                             │ proxies, auth, audit
        ┌──────────────┬──────────────┬──────┴───────┬───────────────┐
        ▼              ▼              ▼              ▼               ▼
    keycloak       mediamtx          nvr           motion       smartsearch
   (identity)   (1 pull/camera,  (record+play)  (event detect)  (search + query)
                 RTSP/HLS fan-out)   ▲              ▲               ▲
        cameras ──RTSP──▶ mediamtx ──┴──────────────┤               │
                                   (all consumers read the relay,   │ observations
                                    never the camera)               │ (POST)
                                                    │               │
                                     frames ────▶ analytics ────────┘
                                (decode once,   (detect person/vehicle/plate;
                                 share frames)   profiles: frames + analytics)
```

**Detection and search are two services.** `analytics` finds the objects;
`smartsearch` stores, ages out and serves them. They are behind separate
compose profiles, and an install running `search` without `analytics` indexes
nothing while every container reports healthy.

## Repository layout

```
docker-compose.yml           the whole stack; host networking on Linux
docker-compose.bridge.yml    overlay for macOS/Windows Docker Desktop
docker-compose.gpu*.yml      GPU overlays (CDI preferred, legacy fallback)
vms / vms.ps1 / vms.cmd      launchers: pick network mode, bootstrap .env/secrets
.env.example                 turnkey config; every value has a working default
deploy/                      mediamtx.yml, Keycloak realm+themes, Caddy TLS, systemd
scripts/                     gen-secrets, rotate-secrets, redis-to-valkey, boot resilience
services/camera-mgmt/        registry + API + SPA host (Python/FastAPI)
services/nvr/                recorder + playback (Python/FastAPI, no auth of its own)
services/motion/             motion detection (Python/FastAPI, no auth of its own)
services/frames/             decode each stream once, share frames (profile: frames)
services/analytics/          person/vehicle/plate detection (profile: analytics)
services/smartsearch/        forensic search index + ANPR (Python/FastAPI)
frontend-react/              the SPA (React + Vite), built into the api image
tests/                       repo-level tests (compose topology)
```

## services/camera-mgmt — the brain and the front door

The only authenticated surface; serves the SPA and proxies every other
service. `backend/main.py` boots it: migrations first (`backend/migrate.py`,
core alembic chain then extension chains), a startup guard that refuses
shipped-default secrets when `DEV_AUTH=false`, then the reconcile loops.

Essential files:

| File | What it contains |
|---|---|
| `backend/main.py` | App wiring, startup order, the insecure-secrets boot guard |
| `backend/config.py` | Every setting (pydantic), env-overridable, loopback defaults |
| `backend/models.py` | SQLAlchemy models + API schemas; `Camera` is the center of gravity (`sub_track`, `search_indexing`, `privacy_masks`, encrypted credentials) |
| `backend/crypto.py` | Fernet encryption of camera credentials at rest (key from `DISCOVERY_SECRET_KEY`) |
| `backend/security.py` | OIDC token validation against Keycloak |
| `backend/urlutil.py` | RTSP credential inject/split + `redact_credentials` for logs |
| `backend/migrate.py` | Core chain, then each extension's own chain (own version table) |
| `migrations/versions/` | Alembic chain 001→037; numbers absent from it belong to an optional extension's own chain and version table |

Routers (`backend/routers/`) — the API surface:

| File | What it contains |
|---|---|
| `cameras.py` | Camera CRUD; the fan-out helpers `_sync_recording_tracks` / `_teardown_recording_tracks` (every change reaches every track); sub-track resolve/enable; privacy masks |
| `discovery.py` | ONVIF WS-Discovery + TCP sweep; credential handling |
| `nvr.py` | Authenticated proxy to the NVR, including the whole-camera purge fan-out (`_purge_all_tracks` — deletes main and `<slug>_sub`, audits truthfully) |
| `search.py` | Smart Search proxy: scopes results to this VMS's cameras, enforces the capability, audits every query |
| `hls.py` | Unauthenticated streaming proxy to MediaMTX HLS (for single-port/tunnel fronts) |
| `motion.py`, `users.py`, `audit.py`, `peripherals.py`, `sitemaps.py` | Motion proxy, Keycloak user admin, audit log, peripherals, site maps |

Services (`backend/services/`) — the outbound half:

| File | What it contains |
|---|---|
| `relay.py` | MediaMTX path control (`add_path`/`patch_path`/`ensure_path` — ensure repoints, add does not) |
| `nvr_client.py` | NVR sync: per-track add/remove, retention, groom (None/0/>0 = default/never/days), codec-cache invalidation, `erase_range_all_tracks` (both tracks, `missing_ok` both ways) |
| `tracks.py` | THE definition of a camera's recording tracks (`tracks_for`), sub retention precedence, `carry_operator_settings` |
| `substream.py` | Sub-stream probing (`resolve_detailed` — UNREACHABLE vs NO_USABLE_SUB, so a transient outage never erases config), bitrate measurement, rtsps `-tls_verify 0` |
| `health.py` | 30s poll: relay/NVR healing, orphan path sweep keyed on `tracks.relay_names` — relay ownership is wider than the recorder's, and keying it on the recorder's deleted live sub-track paths |
| `smartsearch_sync.py` / `smartsearch_client.py` | Feed cameras to the index / query it; pushes each camera's retention RESOLVED to a concrete number (the two services' "defaults" are different values) and reconciles domain + retention drift; empty URL = "no index", a supported deployment |
| `motion_client.py` | Same reconcile shape for the motion service |
| `keycloak_admin.py`, `policy.py`, `audit.py` | User management, capability checks, audit writes |
| `onvif_*.py`, `netscan.py`, `rtsp_verify.py`, `scan_manager.py` | Discovery internals |

Extensions (`backend/extensions/<name>/` — optional packages, discovered by
presence and absent from builds that do not ship them) may add routers and their
own migration chain under their own version table. Core never imports one by
name; an extension that erases footage must go through `erase_range_all_tracks`
so the sub track is erased too.

## services/nvr — recording and playback

Headless recorder; reached only through the api's `/api/nvr` proxy. One ffmpeg
per recording name (`-c copy`, MPEG-TS 60s segments), SQLite index, HLS VOD
playback. A camera with a sub track is simply two recording names
(`slug`, `slug_sub`).

| File | What it contains |
|---|---|
| `main.py` | CLI entry, config, SIGTERM handling |
| `recorder/engine.py` | `RecordingEngine`: one `StreamWorker` per name, health monitor, runtime add/remove, retention/groom maps (groom: None=default, 0=never, >0=days) |
| `recorder/stream_worker.py` | ffmpeg lifecycle, CSV-watermark indexing (filename watermark — survives ffmpeg CSV truncation), reconnect backoff, credential redaction of the command line AND relayed stderr |
| `recorder/masking.py` | Privacy-mask burn-in (re-encode path) |
| `storage/index.py` | Thread-safe SQLite (WAL) segment index |
| `storage/retention.py` | Hourly eviction per recording name + emergency disk floor |
| `storage/grooming.py` | Keyframe-only rewrite of old footage; honours per-camera 0 = never |
| `storage/capacity.py` | Real storage cap ceiling; `detect_virtual_backing` flags WSL2/LinuxKit growable virtual disks whose free space the host cannot back |
| `api/server.py` | Clip extraction, snapshots, coverage; playlist track selection (`covered_seconds` decides, cost breaks ties); TTL'd codec cache + explicit invalidation route |
| `api/hls.py` | HLS VOD: three delivery modes (ts/fmp4/h264) by codec + client capability; `select_track`; artifact cache |

## services/smartsearch — forensic search + ANPR

Registry-pushed cameras, samples the relay at ≤1 FPS, dedups by appearance,
indexes CLIP vectors + crops into searchdb (pgvector). Query API serves
person/vehicle/plate search through the api's `/api/search` proxy.

| File | What it contains |
|---|---|
| `main.py` | Entry; runs `migrations/migrate.py` before serving (migrate && serve) |
| `index/models.py` | `ModelPool`: lazy load, hibernation (release weights when no camera is registered), honest `/health` states |
| `index/pipeline.py`, `index/sampler.py` | Capture threads → bounded queue → single inference worker |
| `index/detector.py` | Person/vehicle detection (ultralytics — the one AGPL dependency, isolated here) |
| `index/embedder.py` | OpenCLIP ViT-B/32 embeddings (512-dim; changing width = re-index) |
| `index/plates.py` | ANPR: open-baseline ONNX localiser (auto-downloads, MIT) or site-tuned weights (`SEARCH_PLATE_WEIGHTS` wins; failed custom load never falls back), fast-plate-ocr recogniser, two-line plate handling |
| `index/motion.py` | Inline motion gate (skip inference on still frames) |
| `index/store.py` | searchdb writes; `expires_at` stamped at insert from the camera's own retention (`set_camera_retention` restamps existing rows), falling back to `SEARCH_RETENTION_DAYS`; batched erase/expire/restamp |
| `index/coverage.py` | Reconciles the index against the NVR's `GET /coverage`; erases rows whose footage is gone. Unknown coverage SKIPS, and a pass taking >50% of a camera refuses itself |
| `index/retention.py` | Hourly expiry sweep + daily coverage sweep + daily orphan-crop sweep, composed into one pass |
| `scripts/purge_unrecorded.py` | The coverage sweep on demand (`--dry-run`, `--camera`, `--force`); shares `index/coverage.py` with the thread |
| `api/server.py` | `/person/search`, `/vehicles/search`, `/plates/search`, `/health` |
| `migrations/` | Plain ordered SQL + `migrate.py` (one-screen runner, not alembic) |

## services/frames — decode once, share the frames

Behind the `frames` profile. One decoder per camera writing into a shared-memory
ring, with subscribers reading rather than each opening their own stream — the
single-pull rule applied a second time, at the decode step instead of the RTSP
one. `docker-compose.broker.yml` (layered automatically when the profile is on)
points motion at it instead of its own decoder.

| File | What it contains |
|---|---|
| `broker/ring.py` | The shared-memory frame ring; the thing subscribers read |
| `broker/engine.py`, `broker/worker.py` | One decode worker per camera, runtime add/remove |
| `broker/notice.py` | How a subscriber learns a frame is ready |
| `api/server.py` | Registry + `/health` (counts frames served, refused, gated) |

## services/analytics — detection

Behind the `analytics` profile. Split out of smartsearch (commit `9f5a5ec`) so
that finding objects and storing them scale separately. Reads frames from the
broker, detects, tracks, picks the best crop per track, reads plates, and POSTs
finished observations to smartsearch's `/observations`.

**`ANALYTICS_SINK_URL` is what makes it useful**, and `docker-compose.
analytics.yml` is what sets it. Without that overlay it detects and sends
nowhere; `./vms` layers it from the active profiles for exactly that reason.

| File | What it contains |
|---|---|
| `analytics/detector.py` | Person/vehicle detection (ultralytics — the AGPL dependency, isolated here) |
| `analytics/tracking.py`, `analytics/selection.py`, `analytics/best_frames.py` | Track objects across frames, keep the best crop per track instead of one per frame |
| `analytics/plates.py` | ANPR localiser + recogniser (smartsearch reports `plates.read_by: analytics`) |
| `analytics/motion.py` | The gate in front of the detector (skip still frames) |
| `analytics/sink.py` | Delivery to smartsearch; at-least-once, client-minted ids |
| `analytics/indexing_policy.py` | What is worth indexing at all |

## services/motion — event detection

Same shape as smartsearch's registry pattern, smaller: `api/server.py` +
`detector/` (frame differencing per camera, events consumed by the api's
`/api/motion` proxy and the events spine).

## frontend-react — the SPA

Vite + React, built inside `services/camera-mgmt/Dockerfile.backend` and
served by the api (one origin, one login). Talks only to `/api/*` (plus the
HLS port for live view over plain HTTP).

| Area | Essential files |
|---|---|
| `src/lib/` | `api.ts` (fetch wrapper, `hlsSrc` — direct :8988 on HTTP, `/hls` proxy on HTTPS), `types.ts`, `auth.ts`, `smartsearch.ts` |
| `src/pages/live/` | `useHls.ts` (hls.js lifecycle, sub-stream fallback), wall/expanded views |
| `src/pages/playback/` | Timeline player over the NVR HLS VOD |
| `src/pages/config/tabs/` | `RecordingTab.tsx` (mode/retention/schedule + the "AI still enabled" banner with one-click stop), `AiTab.tsx` (indexing/motion/activities), `SubTrackCard.tsx` |
| `src/pages/admin/` | `StorageTab.tsx` + `storageRows.ts` (folds `<slug>_sub` bytes into the parent row; purge honesty), users, audit |
| `src/pages/smartsearch/` | Search UI, ANPR panel, result cards |
| `src/extensions/` | Registry for optional UI extensions (nav items, admin tabs, toolbar slots); resolves against whatever is on disk |

## Deploy & scripts

| File | What it contains |
|---|---|
| `vms` | Bash launcher: detects Docker Desktop vs native (daemon-keyed), GPU CDI/legacy detection, first-run `.env` + secrets bootstrap, boot-resilience preflight |
| `vms.ps1` / `vms.cmd` | Windows/macOS PowerShell equivalent (bridge overlay always; secrets bootstrap) |
| `docker-compose.bridge.yml` | Moves every host-network service onto the bridge for Docker Desktop; service-name interconnects; publishes browser ports |
| `deploy/mediamtx.yml` | Relay config: loopback binds on Linux, auth grants |
| `scripts/gen-secrets.*` | First-run secret generation (`--fresh` guarded by a stored-credentials check) |
| `scripts/rotate-secrets.sh` | Live rotation: re-encrypts camera credentials, changes Keycloak first, crash-safe key stash |
| `scripts/redis-to-valkey.sh` | Carries live keys across the Redis→Valkey RDB incompatibility |
| `scripts/install-boot-resilience.sh` | Persistent CDI spec + optional `vms.service` |

## Cross-cutting invariants worth knowing before editing

- **Single-pull rule**: every consumer (NVR, motion, smartsearch, viewers)
  reads the MediaMTX relay, never the camera — one RTSP session per camera,
  and no service except camera-mgmt ever sees camera credentials.
- **Tracks fan out through `tracks.tracks_for`**: anything that changes how a
  camera records must go through the track set, or `<slug>_sub` silently
  diverges (masks, retention, teardown — each was a real bug).
- **Reconcile loops heal, they don't decide**: camera-mgmt re-asserts desired
  state into relay/NVR/motion/smartsearch on an interval; endpoints do the
  immediate change, loops repair drift and restarts.
- **Erasure claims must be true**: purge/erase paths report and audit only
  what actually happened, across both tracks.
- **Secrets never boot on defaults** (`DEV_AUTH=false`), credentials are
  encrypted at rest, and log lines carrying URLs go through credential
  redaction.
- **Tests live next to what they pin**: `services/*/tests/`,
  `frontend-react/src/**/*.test.ts`, and repo-level `tests/`
  (compose topology). All run without the stack via throwaway containers.
