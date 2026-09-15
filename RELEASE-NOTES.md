# Release notes — upgrade guide

Changes an operator of an EXISTING install must know before running
`git pull && ./vms up -d`. Newest first. A fresh clone needs none of this —
the defaults already land where these notes end up.

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
- Check `GET /cameras` on smartsearch: each entry reports `retention_days`.

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
