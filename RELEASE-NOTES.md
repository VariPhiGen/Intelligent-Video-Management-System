# Release notes — upgrade guide

Changes an operator of an EXISTING install must know before running
`git pull && ./vms up -d`. Newest first. A fresh clone needs none of this —
the defaults already land where these notes end up.

## 2026-10-12 — per-camera time-lapse (off by default)

**The upgrade needs nothing from you.** Migration 057 adds `cameras.timelapse_enabled`
(`false` for every existing camera) and `cameras.timelapse_config`, plus two new tables
(`timelapse_frames`, `timelapse_renders`). Nothing is captured until someone switches a
camera on under Configure → Time-lapse.

- **Two new permissions** in Roles & permissions: `timelapse_view` (default: supervisor,
  operator) and `timelapse_manage` (default: supervisor). Admin has both. Viewer and DPO
  get neither until granted.
- **Storage:** stills and videos go to the existing object store (the `vms-heatmaps`
  bucket from compose's MinIO) under a `timelapse/` prefix — no new bucket, no new
  credentials. Without an object store they are kept in the database, so set
  `HEATMAP_S3_*` before switching many cameras on. Each camera has its own retention
  (default 90 days) and size cap (default 5 GB).
- **New dependency:** Pillow in the camera-mgmt image (already pinned for the NVR). The
  image rebuilds on `./vms up -d --build`.
- **Optional env:** `TIMELAPSE_CAPTURE_ENABLED` (appliance kill switch, default true),
  `TIMELAPSE_CAPTURE_CONCURRENCY` (2), `TIMELAPSE_RENDER_RETENTION_DAYS` (30),
  `TIMELAPSE_MAX_RENDER_FRAMES` (20000), `TIMELAPSE_NVENC` (false).

## 2026-09-22 — retention is minute-granular

### Retention is now set in minutes, hours or days

`cameras.retention_days` is `cameras.retention_minutes` (migration 042). A
camera can be kept for twenty minutes, an hour, or thirty days; the Recording
tab and the Storage tab's bulk apply take a number plus a unit. The floor is
physical rather than arbitrary — the recorder writes 60-second segments and
deletes whole ones, so deletion is accurate to about a minute either way.

**The upgrade needs nothing from you.** Migration 042 converts every stored
value exactly (`× 1440`), so no camera's policy changes. `.env` keeps working
untouched: `NVR_DEFAULT_RETENTION_DAYS` and `SEARCH_RETENTION_DAYS` are read
as day-spellings of the same settings. Set `NVR_RETENTION_MINUTES` /
`SEARCH_RETENTION_MINUTES` only if you want a default days cannot express.

- The APIs accept `retention_minutes` (canonical) and `retention_days`
  (deprecated, converted on arrival). **Sending both is a 422** rather than
  one being silently preferred — they would be two different deletion
  deadlines for the same footage.
- `GET /cameras` on camera-mgmt now returns `retention_minutes`,
  `retention_default_minutes` and a human `retention_label`; the old
  `retention_days` / `retention_default_days` response fields are gone. Same on
  smartsearch's `GET /cameras` (`retention_minutes`), and on the NVR's storage
  alert payload (`effective_retention_minutes`, `configured_retention_minutes`).
- The DPDP posture report reports `retention_minutes` plus a label, and the
  CSV/XLSX camera sheets gain a "Retention (minutes)" column beside the
  human one. A camera kept for twenty minutes is a data-minimisation win; a
  report that rounded it to "0 days" would read as a violation.
- **The retention sweep paces itself.** It was a fixed hour, which was fine
  while a day was the finest period expressible — a camera set to ten minutes
  would have held footage for up to seventy. It now runs at the shortest
  configured retention ÷ 4, clamped to [60 s, 1 h], re-read after every pass.
  A fleet of 30-day cameras is still swept hourly, exactly as before. The
  directory walks (orphan, clip and HLS cleanup) stay hourly regardless, so
  shortening one camera does not multiply the I/O cost of every camera.
  Smart Search's expiry sweep paces itself the same way.
- Downgrading past 042 is lossy on purpose: a sub-day retention cannot be
  spelled in whole days, so it floors to 0, which the old code reads as "the
  appliance default". That keeps footage LONGER, visibly, rather than
  inventing a policy nobody set.

## 2026-09 — detection split out of Smart Search; retention follows the camera

### Smart Search indexes NOTHING until you add two profiles

Detection moved out of `smartsearch` into two new services: `frames` (decode
each stream once, share the frames) and `analytics` (detect people, vehicles
and plates, and POST the results to the index). Each is behind its own compose
profile.

**An existing `.env` says `COMPOSE_PROFILES=search`, which after this upgrade
starts an index with nothing feeding it.** Every container reports healthy,
search keeps answering from rows already stored, and the index silently stops
growing. That failure is invisible unless you look for it, so the service now
says so itself — `GET /health` on smartsearch carries:

```json
"warnings": [{"code": "NO_PRODUCER",
              "detail": "nothing is feeding this index ..."}]
```

To adopt:

1. `COMPOSE_PROFILES=search,frames,analytics` in `.env`.
2. `./vms up -d` — it builds the two new images and layers
   `docker-compose.broker.yml` and `docker-compose.analytics.yml` from the
   active profiles. **Do not pass `-f` by hand**: without the analytics
   overlay, `ANALYTICS_SINK_URL` is unset and analytics detects into nowhere,
   which looks exactly like the failure above.
3. Confirm `warnings` is empty and `lifecycle.lifetime.rows_written` climbs.

Dropping all three profiles runs the appliance without Smart Search entirely —
no idle containers, no model images. That is a deliberate choice about what
personal data the appliance collects, which is why the upgrade does not make it
for you.

### Smart Search retention now follows each camera's recording retention

`expires_at` was stamped from one appliance-wide `SEARCH_RETENTION_DAYS`. It is
now stamped from the OWNING CAMERA's retention, pushed by the registry and
resolved to a concrete number of days before it is sent — the NVR's default and
the index's default were different values with nothing keeping them in step.

**Existing rows are restamped, not only new ones.** Retention is normally
shortened after the data exists, so a change that applied to new rows alone
would be a no-op for the case that matters. A camera dropped from 30 days to 2
now moves the deadline on every crop already taken from it, and the next sweep
deletes them.

- Nothing to do on upgrade: with no per-camera override, cameras resolve to
  `NVR_DEFAULT_RETENTION_DAYS` (default 30) and behaviour is unchanged.
- If you set `SEARCH_RETENTION_DAYS` and `NVR_DEFAULT_RETENTION_DAYS` to
  DIFFERENT values on purpose, the recording value now wins for cameras the
  registry knows about. `SEARCH_RETENTION_DAYS` remains the fallback for rows
  from a camera the registry no longer carries.
- Check `GET /cameras` on smartsearch: each entry reports its retention
  (`retention_days` at the time; `retention_minutes` since 2026-09-22).

### The index is reconciled against the recordings automatically

Age was not the only way the index outlived its footage. The recorder and the
indexer restart independently, so a restart skew, a camera re-added under a new
slug, or an NVR erasure that did not reach the index left detections standing
over dead air — hits whose playback 404s, which reads as a bug rather than as
missing footage.

The retention thread now also reconciles against the NVR's `GET /coverage`
(first pass after start, then daily) and erases rows whose footage is gone.

**This deletes index rows and crop files on a timer.** It is bounded three ways,
and all three matter more than the deletion does:

- coverage it cannot LEARN is never treated as absent — an unreachable or
  404ing recorder skips that camera and retries on the next pass;
- rows younger than an hour are never judged, because the in-progress segment
  is not yet in the recorder's index;
- a pass that would erase more than half of one camera's rows refuses itself
  and logs why — a rebuilt `segments.db` looks exactly like lost footage, and
  only one of those is recoverable.

Controls: `SEARCH_RETENTION_COVERAGE_EVERY_N=0` disables it, as does an empty
`NVR_URL`. `GET /health` reports `retention.coverage_sweep_every_n` (null when
off) and the last pass's counts. Run it by hand with
`python3 scripts/purge_unrecorded.py --dry-run`; `--force` overrides the 50%
brake and is deliberately unavailable to the timer.

## 2026-09 — Smart Search on by default, ANPR open baseline

### Smart Search starts with the stack on new installs

`.env.example` now ships `COMPOSE_PROFILES=search`, which starts the
`searchdb` + `smartsearch` services, and the api's `SMARTSEARCH_API_URL` /
`SMARTSEARCH_INDEX_URL` default to that co-located index — so a first start
can search its own footage with no configuration.

**Existing installs are deliberately unchanged**: your `.env` predates the
profile line, so nothing new starts on upgrade. To adopt Smart Search:

1. Add `COMPOSE_PROFILES=search` to `.env`.
2. Delete (or comment out) the old `SMARTSEARCH_API_URL=` and
   `SMARTSEARCH_INDEX_URL=` lines if your `.env` carries them **empty** —
   an explicitly empty value means "this VMS runs no index" and wins over
   the new defaults; an absent variable gets the turnkey default.
3. `./vms up -d` — the first start builds the smartsearch image (~2.4 GB,
   CPU torch) and needs network access for models that download on first use.

### ANPR (plate reading) is active by default wherever Smart Search runs

Plate reading previously required operator-supplied detection weights
(`SEARCH_PLATE_WEIGHTS`) and was otherwise inactive. It now ships with an
open-baseline detector (`open-image-models`, MIT, ONNX) that auto-downloads
on first use — both ANPR stages are MIT-licensed and work out of the box.

**This is a change in what personal data the appliance collects.** A site
that ran Smart Search with plates deliberately inactive will begin reading
and storing licence-plate strings (`search_vehicles.plate`, kept for
`SEARCH_RETENTION_DAYS`, default 30) the first time the upgraded service
loads its models — with no other operator action. The service logs
`plate reading ACTIVE via the open-baseline detector` when this happens.

- **Opt out**: set `SEARCH_PLATE_MODEL=` (explicitly empty) in `.env` —
  plate reading is then inactive and `/health` says so.
- **Site-tuned weights**: `SEARCH_PLATE_WEIGHTS=/models/<file>.pt` beats the
  baseline whenever set (region-trained models, e.g. stacked two-line
  plates, belong here). A failed custom load does NOT fall back to the
  baseline — misconfiguration must not masquerade as mediocrity.

Review your DPDP/privacy documentation before adopting: plate reads are
personal data, and this default changes the collection posture.

## 2026-09 — Redis replaced by Valkey

Valkey 8 cannot read an RDB written by Redis 7.4 (`Can't handle RDB format
version 12`) and the container crash-loops. **Before** switching an existing
appliance, run `scripts/redis-to-valkey.sh --cutover` while the old Redis is
still running, then `--restore` after Valkey is up. If the switch already
happened and Valkey is crash-looping: start a throwaway `redis:7.4` on the
volume, carry the keys (`audit:kc:high_water` is the one that matters — the
Keycloak audit sync resumes from it), clear the RDB, restore.

## 2026-09 — migrations 024/027 moved out of the core chain

A database whose core `alembic_version` is exactly `024` or `027` (it last
migrated when one of those was head) cannot upgrade — the core chain was
reparented and those two revisions are no longer part of it. Re-stamp to the
surviving parent before starting the api:

```sql
UPDATE alembic_version SET version_num='023' WHERE version_num='024';
UPDATE alembic_version SET version_num='026' WHERE version_num='027';
```

Any tables those revisions created are left in place and adopted on the next
boot by whichever chain owns them. Nothing is dropped, and a fresh install
never sees this: it migrates straight to head.

## 2026-09 — boot guard refuses shipped-default secrets

With `DEV_AUTH=false`, the api refuses to start while `INTERNAL_API_KEY`,
`DISCOVERY_SECRET_KEY` or `KEYCLOAK_ADMIN_PASSWORD` hold their shipped
defaults. On an appliance with stored camera credentials, run
`scripts/rotate-secrets.sh --rotate` (it re-encrypts credentials under the
new key and changes Keycloak first); `gen-secrets.sh --fresh` is only for
machines with nothing to lose. Do not work around the guard with
`DEV_AUTH=true` — that makes every caller a full admin.
