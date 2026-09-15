# Smart NVR

![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![Docker](https://img.shields.io/badge/docker-compose-2496ED)
![License](https://img.shields.io/badge/license-AGPL--3.0-green)

**A service inside this repository**, normally started by the root
`docker compose` stack and reached through camera-mgmt's authenticated
`/api/nvr` proxy. The commands below run it on its own, for development.

A lightweight, Linux-based Network Video Recorder built for AI analytics pipelines. Records any number of RTSP camera streams into 60-second MPEG-TS segments with zero transcoding, and exposes a REST API over them: **HLS playlists for playback** (the segments already *are* HLS segments, so a seek costs an HTTP GET rather than an ffmpeg run) plus time-indexed MP4 clip and JPEG snapshot extraction for export, evidence and AI pipelines. A single mid-range server handles 40+ simultaneous streams — the bottleneck is disk I/O, not CPU.

Designed to pair with inference pipelines (DeepStream, Frigate, custom CV models) that detect events and need to retrieve the corresponding footage after the fact.

---

## How It Works

```
RTSP Cameras
     │  (H.264, any resolution/fps)
     ▼
StreamWorker × N         one ffmpeg process per camera
     │  -c copy -f segment -segment_time 60
     │  → {camera}_YYYYMMDD_HHMMSS.ts  (60-second MPEG-TS segments)
     │  → segments.csv                 (completed segment list)
     ▼
SegmentIndex             SQLite WAL — (camera, start_epoch, duration, filepath)
     │
     ├──▶ GET /hls/{cam}/index.m3u8   HLS VOD playlist over the segments (no ffmpeg)
     ├──▶ GET /hls/{cam}/{mode}/{seg}  one segment: raw .ts, or a cached fMP4
     ├──▶ GET /clip       ffmpeg concat/trim → MP4  (< 1 s for 20-second clip)
     ├──▶ GET /snapshot   ffmpeg seek → JPEG frame
     ├──▶ GET /cameras    recording status per camera
     └──▶ GET /health     per-camera liveness + indexer lag

RecordingEngine (monitor thread, 30 s interval)
     ├── restarts dead workers
     ├── logs staleness alarm if lag > 3 × segment_duration
     └── FS↔DB reconciliation every 10 min (self-heals missed segments)

RetentionManager (hourly)
     └── deletes segments older than retention_days and clips older than clip_ttl_minutes
```

---

## Prerequisites

| Requirement | Native | Docker |
|-------------|--------|--------|
| Python | 3.12+ | not needed |
| ffmpeg + ffprobe | must be in PATH | bundled in image |
| Docker + Compose | not needed | v2.x |
| NVIDIA GPU | **not needed** | **not needed** |
| Disk space | ≥ retention × cameras × bitrate | same |

**Storage estimate** (1080p H.264):

| Bitrate | Per camera / day | Per camera / 30 days |
|---------|-----------------|----------------------|
| 4 Mbps  | ~43 GB          | ~1.3 TB              |
| 2 Mbps  | ~22 GB          | ~650 GB              |
| 1 Mbps  | ~11 GB          | ~325 GB              |

---

## Quick Start — Native

```bash
# 1. From a clone of this repository
cd services/nvr

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Configure cameras
cp config/cameras.yaml.example config/cameras.yaml
# Edit cameras.yaml — add your RTSP URLs, camera names, retention_days

# 4. Create storage directories
sudo mkdir -p /data/nvr/clips
# Or change storage_path in cameras.yaml to a path you own

# 5. Start (recording + API)
python main.py --config config/cameras.yaml --port 8080
```

- Browser UI: http://localhost:8080
- OpenAPI docs: http://localhost:8080/docs

**Development mode (API only, no ffmpeg recording):**
```bash
python main.py --no-record --port 8080
```

**Rebuild the SQLite index from existing segment files on disk:**
```bash
python main.py --rebuild-index --config config/cameras.yaml
```

---

## Quick Start — Docker

```bash
# 1. From a clone of this repository
cd services/nvr

# 2. Configure cameras
cp config/cameras.yaml.example config/cameras.yaml
# Edit cameras.yaml

# 3. Start
docker compose up -d

# View logs
docker compose logs -f nvr
```

- API: http://localhost:8009
- Browser UI: http://localhost:8009
- OpenAPI docs: http://localhost:8009/docs

Recording data is stored in the `nvr-data` Docker named volume (survives container restarts).

**If cameras are on the host network and Docker bridge NAT blocks RTSP**, uncomment `network_mode: host` in `docker-compose.yml`.

---

## Configuration Reference

Copy `config/cameras.yaml.example` to `config/cameras.yaml` and edit:

```yaml
cameras:
  - name: entrance          # Used in API calls (?camera=entrance) and as directory name
    rtsp_url: "rtsp://user:pass@192.168.1.10:554/Streaming/Channels/101"
    retention_days: 7       # Segments older than this are deleted hourly

recording:
  segment_duration: 60      # Seconds per .ts file (60 = 1 440 files/day/camera)
  storage_path: /data/nvr   # Root for all camera subdirectories + segments.db
  clips_path: /data/nvr/clips
  clip_ttl_minutes: 30      # Extracted clips are auto-deleted after this

ffmpeg:
  rtsp_transport: tcp       # tcp (reliable) or udp (lower latency)
  reconnect_delay: 5        # Base seconds before reconnecting; doubles on each failure
  max_reconnect_attempts: 0 # 0 = retry forever
  loglevel: warning
```

> **If integrating with DeepStream**: the `name` field for each camera must exactly match the `nvr_camera_name` field in the DeepStream pipeline's Qdrant payloads. This is the join key used by the search API to map a person sighting to a clip request.

---

## API Reference

All timestamps are ISO 8601. Responses are JSON unless the endpoint returns a file.

### `GET /hls/{camera}/index.m3u8` — Playback playlist (the fast path)

MPEG-TS *is* the HLS segment format, so playback of recorded footage needs no
clip build at all: this renders the segment index as an HLS VOD playlist and the
player fetches only the minute it is about to show. Seeking costs an HTTP GET
instead of an ffmpeg run, and nothing is rebuilt when you scrub back.

```
GET /hls/{camera}/index.m3u8?from=<ISO8601>&to=<ISO8601>&hevc=<bool>
```

`hevc` is the *client's* answer to "can you decode HEVC" (the SPA probes
`MediaSource.isTypeSupported`; never infer it from the User-Agent). Together
with the recorded codec it picks one of three delivery modes, reported back in
the `X-NVR-HLS-Mode` response header:

| Mode | When | Server cost per 60 s segment |
|---|---|---|
| `ts` | H.264 source | none — the recorded file is served as-is |
| `fmp4` | HEVC source, HEVC-capable client | ~0.06 s (`-c copy` remux) |
| `h264` | HEVC source, everything else | a transcode, but cached and reused |

Windows are capped at `recording.max_clip_seconds`. Built artifacts are cached
under `clips_path/hls`, keyed on the source segment's size+mtime so grooming
can never serve stale footage, and re-checked against the index on every
request so erased footage stops being reachable immediately.

### `GET /clip` — Extract a video clip

Returns an MP4 file centered on the requested timestamp.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `camera` | string | required | Camera name from config |
| `timestamp` | string | required | ISO 8601 (e.g. `2026-03-17T14:32:10Z`) |
| `before` | float | 10.0 | Seconds before timestamp (0–300) |
| `after` | float | 10.0 | Seconds after timestamp (0–300) |

**Response headers:**
- `X-NVR-Coverage`: fraction of the requested window covered by recordings (`1.0` = full, `0.7` = 30% gap)
- `X-NVR-Warning`: present when coverage < 100%

```bash
# Default 20-second clip (10s before + 10s after)
curl "http://localhost:8080/clip?camera=entrance&timestamp=2026-03-17T14:32:10Z" \
  -o clip.mp4

# Custom window: 30s before, 5s after
curl "http://localhost:8080/clip?camera=entrance&timestamp=2026-03-17T14:32:10Z&before=30&after=5" \
  -o clip.mp4
```

### `GET /snapshot` — Extract a JPEG frame

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `camera` | string | required | Camera name |
| `timestamp` | string | required | ISO 8601 |
| `quality` | int | 85 | JPEG quality 1–95 |

```bash
curl "http://localhost:8080/snapshot?camera=entrance&timestamp=2026-03-17T14:32:10Z" \
  -o frame.jpg
```

### `GET /cameras` — List cameras

Returns all cameras with recording status, time range, and storage usage.

```bash
curl http://localhost:8080/cameras
```

```json
{
  "cameras": [
    {
      "name": "entrance",
      "earliest": "2026-03-17T00:00:00+00:00",
      "latest": "2026-03-17T14:35:00+00:00",
      "storage_bytes": 5368709120,
      "recording": true,
      "last_indexed_at": "2026-03-17T14:34:58+00:00",
      "index_lag_seconds": 2.1,
      "segments_indexed": 864
    }
  ]
}
```

### Other endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/cameras/{name}/range` | Recording time range for a single camera |
| `POST` | `/cameras/{name}?rtsp_url=&retention_days=` | Add a camera at runtime (no restart needed) |
| `DELETE` | `/cameras/{name}` | Stop recording and remove a camera at runtime |
| `GET` | `/health` | Per-camera: `alive`, `last_indexed_at`, `index_lag_seconds`, `segments_indexed` |
| `GET` | `/storage` | Per-camera and total storage in bytes and GB |
| `GET` | `/docs` | OpenAPI interactive documentation |
| `GET` | `/` | Browser UI |

---

## CLI Flags

```
python main.py [OPTIONS]

  -c, --config PATH     Camera config YAML (default: config/cameras.yaml)
  --host HOST           API listen address (default: 0.0.0.0)
  --port PORT           API listen port (default: 8080)
  --no-record           Start API only — no ffmpeg recording (useful for development)
  --rebuild-index       Scan segment files on disk and rebuild the SQLite index before starting
```

---

## Failure Recovery

| Scenario | Behavior |
|----------|----------|
| Camera stream drops | ffmpeg exits → exponential backoff reconnect (5 s → 10 s → … → 60 s cap, +25% jitter) → resets to base on first successful segment |
| NVR process crashes | `.ts` files on disk are valid (TS has no moov atom requirement). Restart normally; use `--rebuild-index` if the SQLite index was lost. |
| Indexer stalls silently | Health monitor logs a staleness warning if no segment was indexed for `3 × segment_duration`. FS↔DB reconciliation every 10 min self-heals any segments written to disk but missing from the index. |
| Clip requested during a gap | Returns whatever footage exists in the window with `X-NVR-Coverage < 1.0` in the response headers. |
| Disk full | ffmpeg exits → reconnect loop. Monitor `/storage` and set `retention_days` accordingly. |
| SQLite index lost/corrupted | Run `python main.py --rebuild-index --config config/cameras.yaml` |

---

## Adding a Camera at Runtime

No restart required:

```bash
curl -X POST "http://localhost:8080/cameras/parking-lot?rtsp_url=rtsp://admin:pass@192.168.1.20:554/stream&retention_days=14"
```

To make it permanent, add the entry to `config/cameras.yaml`.

---

## Integrating with an AI Pipeline

```python
import requests
from datetime import datetime, timezone

def fetch_clip(camera: str, event_time: datetime,
               before: float = 10.0, after: float = 10.0,
               nvr_url: str = "http://localhost:8080") -> bytes:
    resp = requests.get(f"{nvr_url}/clip", params={
        "camera": camera,
        "timestamp": event_time.isoformat(),
        "before": before,
        "after": after,
    }, timeout=15)
    resp.raise_for_status()
    coverage = float(resp.headers.get("X-NVR-Coverage", "1.0"))
    if coverage < 1.0:
        print(f"Warning: only {coverage:.0%} of the requested window was recorded")
    return resp.content  # MP4 bytes — feed to cv2.VideoCapture or decord
```

**Clip format for CV models:**
- Container: MP4, `-movflags +faststart` (streaming-friendly)
- Video: H.264 (or whatever the camera outputs — no transcode)
- Audio: none (`-an` dropped at recording time)
- Resolution / FPS: native camera output
- For OpenCV: `cv2.VideoCapture(clip_path)` works directly
- For PyTorch: `torchvision.io.read_video()` or `decord.VideoReader`

---

## License

MIT
