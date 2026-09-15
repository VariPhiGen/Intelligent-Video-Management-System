# camera-mgmt — the VMS API, and the RTSP relay it manages

Accepts external RTSP camera URLs and re-streams them as stable local RTSP URLs
over LAN/WAN.  Scales to thousands of concurrent streams using MediaMTX as the
relay engine with no per-stream FFmpeg processes for pass-through streams.

```
rtsp://admin:pass@192.168.1.10/stream  →  rtsp://<server-ip>:8654/lobby-cam-a3f1
```

---

## Architecture

```
IP Cameras  ──pull──►  MediaMTX :8654  ──serve──►  DeepStream / VMS / any client
                            ▲
                      REST API :9997
                            │
                  FastAPI :8091 (+ SPA UI + /docs)
                       ┌─────┴──────┐
                   PostgreSQL    Redis
```

See [architecture.md](architecture.md) for the full component diagram and
scaling analysis.

---

## Prerequisites

| Requirement | Version |
|-------------|---------|
| Ubuntu | 22.04 LTS |
| Docker + Docker Compose | ≥ 24.0 |
| Server with static IP on the camera LAN | — |
| FFmpeg (for snapshot endpoint) | Installed in Docker image |

For native (non-Docker) deployment: Python 3.11+, ffmpeg, PostgreSQL 15, Redis 7,
and [MediaMTX](https://github.com/bluenviron/mediamtx/releases) binary.

---

## Quick Start (Docker)

### 1. Clone and configure

```bash
git clone <repo> rtsp-relay && cd rtsp-relay
cp .env.example .env
# Edit .env — set SERVER_IP to this server's LAN IP
nano .env
```

Minimum required changes in `.env`:
```
SERVER_IP=192.168.1.100        # This server's IP (used in returned RTSP URLs)
POSTGRES_PASSWORD=your_secret
DATABASE_URL=postgresql+asyncpg://rtsp:your_secret@127.0.0.1:5432/rtsp_relay
```

### 2. Start all services

```bash
docker compose up -d
```

Services start in order: postgres → redis → mediamtx → api.
The API container runs `alembic upgrade head` before uvicorn starts, then serves
the REST API (including native ONVIF camera discovery under /api/discovery/*)
and the management UI on port 8091 (the default — set `API_PORT` to change it;
every example below uses the 8091 default).

### 3. Verify

```bash
# Overall health
curl http://localhost:8091/health | jq

# Open the management UI (served by FastAPI, same port as the API)
xdg-open http://localhost:8091     # or browse to http://<server-ip>:8091
```

---

## Adding Cameras

### Via the UI

Open `http://<server-ip>` in a browser.  Click **+ Add Camera**, enter the
camera name and RTSP URL, and click **Add Camera**.

The local RTSP URL is immediately available at `rtsp://<SERVER_IP>:8654/<slug>`.

### Via the API

```bash
curl -X POST http://localhost:8091/api/cameras \
  -H 'Content-Type: application/json' \
  -d '{"name": "Lobby Camera", "rtsp_url": "rtsp://admin:pass@192.168.1.10/stream1"}'
```

Response:
```json
{
  "id": "...",
  "name": "Lobby Camera",
  "slug": "lobby-camera-a3f1",
  "local_rtsp_url": "rtsp://192.168.1.100:8654/lobby-camera-a3f1",
  "health_status": "unknown",
  ...
}
```

### With a custom slug

```bash
curl -X POST http://localhost:8091/api/cameras \
  -H 'Content-Type: application/json' \
  -d '{"name": "Gate 2", "rtsp_url": "rtsp://...", "slug": "gate-2"}'
```

---

## Bulk Upload

### CSV format

```csv
name,rtsp_url,slug
Lobby Camera,rtsp://admin:pass@192.168.1.10/stream1,
Gate 2,rtsp://admin:pass@192.168.1.11/stream1,gate-2
Parking Lot,rtsp://viewer:1234@10.0.0.5:554/h264,
```

- `slug` column is optional — leave blank for auto-generation
- First row must be the header

```bash
# Dry run (validate without inserting)
curl -X POST 'http://localhost:8091/api/cameras/bulk?dry_run=true' \
  -F 'file=@cameras.csv'

# Live upload
curl -X POST 'http://localhost:8091/api/cameras/bulk' \
  -F 'file=@cameras.csv'
```

### JSON format

```bash
curl -X POST 'http://localhost:8091/api/cameras/bulk' \
  -H 'Content-Type: application/json' \
  -d '{
    "cameras": "[{\"name\":\"Lobby\",\"rtsp_url\":\"rtsp://admin:pass@192.168.1.10/stream1\"},{\"name\":\"Gate\",\"rtsp_url\":\"rtsp://admin:pass@192.168.1.11/stream1\",\"slug\":\"gate\"}]"
  }'
```

Bulk endpoint always validates all rows first.  If any row has a validation
error, **no rows are inserted** and the per-row errors are returned.

---

## Consuming Local RTSP URLs

Once a camera is registered, its local RTSP URL is ready for any client:

### NVIDIA DeepStream

In `config_media.json`:
```json
{
  "rtsp_url": "rtsp://192.168.1.100:8654/lobby-camera-a3f1",
  "camera_name": "lobby-camera-a3f1"
}
```

### FFmpeg

```bash
ffplay rtsp://192.168.1.100:8654/lobby-camera-a3f1
```

### GStreamer

```bash
gst-launch-1.0 rtspsrc location=rtsp://192.168.1.100:8654/lobby-camera-a3f1 ! decodebin ! autovideosink
```

---

## Camera Management API

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/cameras` | Add a single camera |
| POST | `/api/cameras/bulk` | Bulk upload (CSV or JSON) |
| GET | `/api/cameras` | List cameras (supports `?enabled=true&skip=0&limit=100`) |
| GET | `/api/cameras/{id}` | Get camera detail |
| PUT | `/api/cameras/{id}` | Update camera (name, URL, enabled state) |
| DELETE | `/api/cameras/{id}` | Remove camera and stop relay |
| POST | `/api/cameras/{id}/reconnect` | Force-reconnect a stream |
| GET | `/api/cameras/{id}/health` | Per-stream health (status, tracks, reconnects) |
| GET | `/api/cameras/{id}/snapshot` | JPEG thumbnail of current frame |
| GET | `/health` | System health (all services) |

Interactive API docs: `http://<server-ip>:8091/docs` · Management UI: `http://<server-ip>:8091/`

---

## Configuration

All settings are in `.env` (see [`.env.example`](.env.example)).  Key tuneables:

| Variable | Default | Effect |
|----------|---------|--------|
| `SERVER_IP` | `127.0.0.1` | IP embedded in returned RTSP URLs |
| `API_PORT` | `8091` | Port the API + SPA + docs listen on (single source of truth) |
| `RTSP_PORT` | `8654` | MediaMTX RTSP port (not 8554, so it can coexist with another MediaMTX on the host) |
| `HEALTH_POLL_INTERVAL` | `30` | Seconds between health checks |
| `MAX_TRANSCODE_WORKERS` | `8` | Max concurrent FFmpeg processes |
| `LOG_LEVEL` | `info` | Structured JSON log verbosity |

---

## Native (Non-Docker) Deployment

```bash
# 1. Install system deps
sudo apt install -y python3.11 python3.11-venv ffmpeg

# 2. Download MediaMTX binary
wget https://github.com/bluenviron/mediamtx/releases/download/v1.9.1/mediamtx_v1.9.1_linux_amd64.tar.gz
sudo tar xzf mediamtx_v*.tar.gz -C /usr/local/bin

# 3. Setup Python venv
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 4. Configure
cp .env.example .env && nano .env

# 5. Run migrations
.venv/bin/alembic upgrade head

# 6. Start MediaMTX (config lives at the repo root: deploy/mediamtx.yml)
mediamtx ../../deploy/mediamtx.yml &

# 7. Start FastAPI (honors API_PORT from .env; defaults to 8091)
.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port "${API_PORT:-8091}" --workers 4
```

---

## Health Monitoring & Auto-Reconnect

The background health monitor runs every `HEALTH_POLL_INTERVAL` seconds.

- **Connected**: MediaMTX reports an active source → Redis `stream:health:{slug}` updated, `last_seen_at` refreshed in PostgreSQL.
- **Disconnected**: Source lost → exponential backoff reconnect (5 → 10 → 30 → 60 → 120 → 300 seconds).
- **Error**: Path disappeared from MediaMTX entirely → re-registers path immediately.

MediaMTX also has its own built-in reconnect for RTSP sources.  The application-level
monitor provides a second safety net and surfaces health status to the API.

Force a manual reconnect:
```bash
curl -X POST http://localhost:8091/api/cameras/{id}/reconnect
```

---

## Scaling

For deployments beyond a single server, see the **Scale Model** section in
[architecture.md](architecture.md).

**Rough capacity (pass-through only):**

| Server | RAM | Approximate max streams |
|--------|-----|------------------------|
| 4-core / 8 GB | — | ~3,000 |
| 16-core / 32 GB | — | ~10,000+ |
| 32-core / 64 GB | — | ~20,000+ |

Pass-through streams (H.264/H.265 stream copy) consume ~2 MB RAM and negligible
CPU per stream in MediaMTX.  Transcoding streams consume 80–200 MB RAM and
0.5–2 CPU cores each; these are limited by `MAX_TRANSCODE_WORKERS`.

---

## Troubleshooting

**Stream shows "Disconnected" immediately after adding**
- Check the camera RTSP URL is reachable from the server: `ffprobe rtsp://...`
- Check MediaMTX logs: `docker compose logs mediamtx -f`
- Verify network/firewall rules allow outbound RTSP (TCP 554)

**`host.docker.internal` not resolving**
- Ensure Docker ≥ 20.10 and the `extra_hosts` entry in `docker-compose.yml`
- Fall back to `172.17.0.1` (default Docker bridge gateway) if needed

**Alembic migration fails**
- Check `DATABASE_URL` is correct and PostgreSQL is running
- Check PostgreSQL user has `CREATEDB` / `CREATE EXTENSION` privileges

**Snapshot returns 503**
- The stream must be `connected` for a snapshot; check health status first
- FFmpeg must be installed in the API container (it is in `Dockerfile.backend`)

**High memory usage**
- Each transcoding sidecar uses 80–200 MB; reduce `MAX_TRANSCODE_WORKERS`
- For pure pass-through streams no FFmpeg process is spawned; MediaMTX uses ~2 MB/stream
