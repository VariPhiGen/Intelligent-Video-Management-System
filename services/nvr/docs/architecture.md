# Smart NVR — Exhaustive Technical Architecture

This document describes every timing interval, data flow, thread, and failure/recovery path in the system. It is intended to be complete enough that an engineer can understand the full internals without reading source code.

---

## 1. System Startup Sequence

```
main.py
  │
  ├─ argparse → (config_path, host, port, no_record, rebuild_index)
  │
  ├─ load_config(cameras.yaml)
  │     └─ returns dict: {cameras[], recording{}, ffmpeg{}}
  │
  ├─ mkdir -p  storage_path   (/data/nvr)
  ├─ mkdir -p  clips_path     (/data/nvr/clips)
  │
  ├─ SegmentIndex(db_path=/data/nvr/segments.db)
  │     ├─ sqlite3.connect(db_path, timeout=30)
  │     ├─ PRAGMA journal_mode=WAL
  │     ├─ PRAGMA synchronous=NORMAL
  │     └─ CREATE TABLE IF NOT EXISTS segments(...)
  │         CREATE INDEX IF NOT EXISTS idx_camera_start(camera, start_epoch)
  │         CREATE INDEX IF NOT EXISTS idx_camera_end(camera, start_epoch+duration)
  │
  ├─ [if --rebuild-index]
  │     └─ index.rebuild_from_filesystem(storage_path, StreamWorker._probe_duration)
  │           └─ rglob("*.ts") → sorted → for each:
  │                 _probe_duration(filepath) → ffprobe → float seconds
  │                 Segment(camera=parent_dir_name, start_epoch, duration, filepath, size)
  │                 index.add_segment(seg)  [INSERT OR REPLACE]
  │
  ├─ [if not --no-record]
  │     RecordingEngine(config, index)
  │       ├─ for each camera in config["cameras"]:
  │       │     StreamWorker(camera_name, rtsp_url, storage_path, index,
  │       │                  segment_duration, rtsp_transport,
  │       │                  reconnect_delay, ffmpeg_loglevel)
  │       └─ engine.start()
  │             ├─ worker.start()  ×N  (spawns thread rec-{camera})
  │             └─ Thread(target=_health_monitor, name="rec-monitor").start()
  │
  ├─ RetentionManager(index, cameras, clips_path, clip_ttl_minutes, storage_path)
  │     └─ Thread(target=run_retention_loop, name="retention", daemon=True).start()
  │
  ├─ create_app(index, clips_path, engine) → FastAPI instance
  │     ├─ sets module globals: _index, _clips_path, _engine
  │     ├─ registers routes: /clip /snapshot /cameras /cameras/{cam}/range
  │     │                     /cameras/{cam} POST/DELETE /health /storage
  │     └─ mounts StaticFiles(ui/) at "/"  [catch-all, must be last]
  │
  ├─ signal.signal(SIGTERM / SIGINT → shutdown handler)
  │     └─ shutdown: engine.stop() → stop_event.set() → sys.exit(0)
  │
  └─ uvicorn.run(app, host, port)   ← main thread blocks here
```

---

## 2. Thread Inventory

```
┌─────────────────────────────────────────────────────────────────┐
│ Process: python main.py                                         │
│                                                                 │
│  main thread ────────────────── uvicorn asyncio event loop      │
│                                 (handles all HTTP requests)     │
│                                                                 │
│  rec-entrance   (daemon) ─────── StreamWorker._run_loop()       │
│  rec-office-cam01 (daemon) ───── StreamWorker._run_loop()       │
│  rec-office-cam02 (daemon) ───── StreamWorker._run_loop()       │
│  rec-office-cam03 (daemon) ───── StreamWorker._run_loop()       │
│  ... (one per configured camera)                                │
│                                                                 │
│  rec-monitor    (daemon) ─────── RecordingEngine._health_monitor│
│  retention      (daemon) ─────── run_retention_loop()           │
│                                                                 │
│  stderr-{camera} (daemon) ────── StreamWorker._log_stderr()     │
│  (one per ACTIVE ffmpeg process, replaced on each reconnect)   │
└─────────────────────────────────────────────────────────────────┘
```

**SQLite thread safety**: `SegmentIndex` uses `threading.local()` — each thread gets its own `sqlite3.Connection`. WAL mode allows concurrent reads from the API event loop while recorder threads write.

---

## 3. StreamWorker: Recording Loop

```
StreamWorker._run_loop()
  │
  │  ┌──────────────────────────────────────────────┐
  │  │             ffmpeg session (_record)          │
  │  │                                              │
  │  │  ffmpeg subprocess                           │
  │  │    -rtsp_transport  tcp                      │
  │  │    -timeout         5000000    (5 s in µs)   │
  │  │    -i               rtsp://...               │
  │  │    -c:v             copy       (no transcode) │
  │  │    -an                         (drop audio)   │
  │  │    -f               segment                  │
  │  │    -segment_time    60         (seconds)      │
  │  │    -segment_format  mpegts                   │
  │  │    -segment_list    segments.csv             │
  │  │    -segment_list_type csv                    │
  │  │    -segment_list_size 0        (keep full)   │
  │  │    -strftime        1          (timestamp in filename) │
  │  │    -reset_timestamps 1                       │
  │  │    -break_non_keyframes 1                    │
  │  │    entrance_%Y%m%d_%H%M%S.ts                 │
  │  │                                              │
  │  │  stdout: (not captured)                      │
  │  │  stderr: → _log_stderr thread (debug level)  │
  │  │                                              │
  │  │  _watch_segment_list(segments.csv)  ─────────┼──▶ see section 4
  │  │                                              │
  │  │  process.wait()                              │
  │  └──────────────────────────────────────────────┘
  │
  ├─ [if stop_event.set]: break → worker exits cleanly
  │
  ├─ delay = current_backoff + uniform(0, 0.25 × current_backoff)
  │   └─ stop_event.wait(delay)   ← interruptible sleep
  │
  └─ current_backoff = min(current_backoff × 2.0, 60.0)
       └─ then loop: spawn new ffmpeg session
```

---

## 4. Segment Indexing — CSV Watcher

```
StreamWorker._watch_segment_list(csv_path)
  │
  │  Called while ffmpeg subprocess is alive (_process.poll() is None)
  │  Polling interval: 2 seconds
  │
  │  On each tick:
  │  ├─ os.path.exists(csv_path) ?  No → sleep 1s, retry
  │  │
  │  └─ open(csv_path, "r") → readlines()
  │       │
  │       └─ for each line:
  │             filename = line.split(",")[0].strip()
  │             base = os.path.basename(filename)
  │             │
  │             ├─ base <= self._last_indexed_filename  ?  SKIP  (already processed)
  │             │   (lexical comparison = chronological because filenames are
  │             │    timestamp-encoded: camera_YYYYMMDD_HHMMSS.ts)
  │             │
  │             └─ _index_segment_line(line)
  │                   │
  │                   ├─ parse: filename, start_pts, end_pts  (3 CSV fields)
  │                   │   bad format → WARNING, return (do not advance watermark)
  │                   │
  │                   ├─ resolve filepath (absolute or relative to segment_dir)
  │                   │
  │                   ├─ filepath.exists() ?
  │                   │   No → WARNING "flush race" → return (do not advance watermark)
  │                   │       (will retry on next 2-second poll tick)
  │                   │
  │                   ├─ _parse_filename_time(filename)
  │                   │   regex: (\d{8})_(\d{6})\.ts$
  │                   │   strptime "%Y%m%d%H%M%S" → datetime(UTC) → .timestamp()
  │                   │   None → WARNING, return
  │                   │
  │                   ├─ duration = end_pts - start_pts
  │                   │   if ≤ 0 or ValueError → _probe_duration(filepath)
  │                   │     ffprobe -show_entries format=duration → float
  │                   │     None or ≤ 0 → WARNING, return
  │                   │
  │                   ├─ index.add_segment(Segment(camera, start_epoch, duration,
  │                   │                            filepath, file_size))
  │                   │   SQL: INSERT OR REPLACE INTO segments VALUES (...)
  │                   │        conn.commit()
  │                   │
  │                   └─ _mark_indexed(base)
  │                         ├─ self._last_indexed_filename = base  (advance watermark)
  │                         ├─ self._last_indexed_at = time.time()
  │                         ├─ self._segments_indexed += 1
  │                         └─ if current_backoff != base: reset to base + log INFO
```

**Why filename watermark, not line count:**
ffmpeg's segment muxer opens `segments.csv` with `O_TRUNC` on every new invocation. After a stream reconnect, the file has fewer lines than before, so a line-count check (`len(lines) > last_line_count`) never fires — the indexer silently stalls until the count exceeds the old high-water mark. The filename watermark is immune to this because it compares content, not length.

---

## 5. Reconnection Backoff State Machine

```
                     ┌─────────────────────────────────┐
                     │  RECORDING (ffmpeg running)      │
                     │  current_backoff = base (5s)     │
                     └────────────┬────────────────────┘
                                  │
                         ffmpeg exits (stream drop,
                         network error, timeout)
                                  │
                     ┌────────────▼────────────────────┐
                     │  BACKING OFF                     │
                     │  delay = current_backoff         │
                     │        + uniform(0, 0.25×cb)     │
                     │  stop_event.wait(delay)          │
                     └────────────┬────────────────────┘
                                  │  delay elapsed
                     ┌────────────▼────────────────────┐
                     │  RECONNECTING                    │
                     │  current_backoff = min(cb×2, 60) │
                     │  spawn new ffmpeg subprocess     │
                     └────────────┬────────────────────┘
                                  │
                    ┌─────────────┴──────────────┐
                    │                            │
              first segment               ffmpeg exits again
              indexed                           │
                    │                    (continue backoff loop)
          ┌─────────▼──────────┐
          │  current_backoff   │
          │  reset to base(5s) │
          └─────────┬──────────┘
                    │
          ┌─────────▼──────────┐
          │  RECORDING (healthy)│
          └────────────────────┘

Backoff sequence on consecutive failures:
  5s → 10s → 20s → 40s → 60s (cap) → 60s → 60s → ...
  (each with +0 to +25% random jitter)
```

---

## 6. RecordingEngine Health Monitor

```
Thread: rec-monitor   interval: HEALTH_CHECK_INTERVAL = 30 s

Every 30 seconds:
  │
  ├─ [1] LIVENESS CHECK
  │   for each (name, worker):
  │     worker.is_alive  =  _running AND _thread.is_alive()
  │     if not alive:
  │       worker.stop()    (terminate ffmpeg, join thread, timeout 15s)
  │       worker.start()   (new thread + new ffmpeg)
  │       log WARNING: "Worker for '{name}' is dead, restarting"
  │
  ├─ [2] STALENESS ALARM  (informational — no auto-restart)
  │   staleness_threshold = STALENESS_FACTOR(3) × segment_duration(60) = 180 s
  │   for each (name, worker):
  │     lag = now - worker.last_indexed_at   (None if never indexed)
  │     if lag is None: skip  (could be cold start or camera offline since boot)
  │     if lag > 180:
  │       log WARNING: "Camera '{name}': indexer stale — last indexed {lag:.1f}s ago"
  │     (No restart: stalled indexer ≠ offline camera. A healthy ffmpeg
  │      writing segments is a different problem from a dead process.)
  │
  └─ [3] FS↔DB RECONCILIATION  (every RECONCILIATION_INTERVAL = 600 s)
      if now - last_reconcile_at >= 600:
        _reconcile_all()
          for each (name, worker):
            worker.reconcile(lookback_seconds=3600)
              │
              ├─ get_indexed_filepaths_since(camera, now-3600)
              │   SQL: SELECT filepath WHERE camera=? AND start_epoch >= ?
              │   → set of already-indexed paths
              │
              ├─ glob *.ts in segment_dir
              │
              └─ for each .ts file:
                   skip if already in indexed set
                   parse start_epoch from filename
                   skip if start_epoch < cutoff (older than lookback)
                   skip if mtime < 5s ago  (ffmpeg still writing)
                   _probe_duration(filepath)  → ffprobe
                   add_segment(...)  [INSERT OR REPLACE — idempotent]
                   _mark_indexed(filename)
                   log WARNING: "reconciliation indexed missing segment"
```

---

## 7. RetentionManager

```
Thread: retention   interval: 3600 s (1 hour)

run_once():
  │
  ├─ SEGMENT RETENTION (per camera)
  │   for each camera in config["cameras"]:
  │     cutoff = now - retention_days × 86400
  │     filepaths = index.delete_before(camera, cutoff)
  │       SQL: SELECT filepath WHERE camera=? AND (start_epoch+duration) < ?
  │            DELETE WHERE camera=? AND (start_epoch+duration) < ?
  │            conn.commit()
  │     for path in filepaths:
  │       os.unlink(path)  [ignore FileNotFoundError]
  │
  └─ CLIP RETENTION
      cutoff = now - clip_ttl_minutes × 60
      for file in clips_path.iterdir():
        if file.stat().st_mtime < cutoff:
          file.unlink()
```

---

## 8. API — Clip Extraction (`GET /clip`)

```
Request: GET /clip?camera=entrance&timestamp=2026-03-17T14:32:10Z&before=10&after=10

FastAPI (async, uvicorn event loop)
  │
  ├─ parse timestamp → datetime.fromisoformat → ts_epoch (float, Unix)
  │   invalid → HTTP 400
  │
  ├─ _index.get_cameras() → [list of cameras with segments]
  │   camera not in list → HTTP 404 + available_cameras
  │
  ├─ clip_start = ts_epoch - before    (ts - 10s)
  │   clip_end   = ts_epoch + after    (ts + 10s)
  │   clip_duration = before + after   (20s)
  │
  ├─ _index.get_recording_range(camera)
  │   SQL: SELECT MIN(start_epoch), MAX(start_epoch+duration) WHERE camera=?
  │   None → HTTP 404 "no recordings"
  │   clip_end < earliest OR clip_start > latest → HTTP 404 + range info
  │
  ├─ _index.find_segments(camera, clip_start, clip_end)
  │   SQL: SELECT ... WHERE camera=?
  │              AND start_epoch < clip_end
  │              AND (start_epoch+duration) > clip_start
  │         ORDER BY start_epoch ASC
  │   empty → HTTP 404 "gap in recording"
  │
  ├─ filter: [s for s in segments if os.path.exists(s.filepath)]
  │   all missing → HTTP 500 "segment files missing"
  │
  ├─ clip_filename = f"{camera}_{int(ts_epoch)}_{uuid4().hex[:8]}.mp4"
  │   clip_path = clips_path / clip_filename
  │
  ├─ _extract_clip(segments, clip_start, clip_duration, clip_path)
  │   │
  │   ├─ [single segment]
  │   │     seek_offset = clip_start - segment.start_epoch
  │   │     ffmpeg -ss {seek_offset} -i {seg.filepath}
  │   │            -t {clip_duration} -c copy
  │   │            -movflags +faststart -y {output}
  │   │     timeout: 30s
  │   │
  │   └─ [multiple segments]
  │         seek_offset = clip_start - segments[0].start_epoch
  │         write tempfile:
  │           file '/data/nvr/entrance/entrance_20260317_143200.ts'
  │           file '/data/nvr/entrance/entrance_20260317_143300.ts'
  │         ffmpeg -f concat -safe 0
  │                -ss {seek_offset} -i {tempfile}
  │                -t {clip_duration} -c copy
  │                -movflags +faststart -y {output}
  │         timeout: 30s
  │         finally: os.unlink(tempfile)
  │
  │   ffmpeg returncode != 0 → ClipExtractionError → HTTP 500 + stderr
  │
  ├─ clip_path.exists() AND size > 0 ?  No → HTTP 500
  │
  ├─ _compute_coverage(segments, clip_start, clip_end)
  │   for each seg: covered += min(seg.end, clip_end) - max(seg.start, clip_start)
  │   coverage = covered / (clip_end - clip_start)
  │   if < 1.0: headers["X-NVR-Coverage"] = f"{coverage:.2f}"
  │             headers["X-NVR-Warning"]  = "Partial coverage..."
  │
  └─ FileResponse(clip_path, media_type="video/mp4", headers=headers)
       └─ serves the file; RetentionManager deletes it after clip_ttl_minutes
```

**Seeking accuracy:** `-ss` as an input option seeks to the nearest keyframe before the target, then `-c copy` outputs from there. Accuracy is ±1 keyframe interval (typically 1–2 s for 1080p H.264 at default GOP settings). Frame-exact accuracy would require `-c:v libx264` re-encode (10–50× slower) and is not needed for AI inference pipelines.

---

## 9. API — Snapshot Extraction (`GET /snapshot`)

```
Request: GET /snapshot?camera=entrance&timestamp=2026-03-17T14:32:10Z&quality=85

  ├─ [validation: same as /clip — parse ts, check camera, check range]
  │
  ├─ find_segments(camera, ts_epoch, ts_epoch)   ← exact point query
  │   if empty: retry with (ts_epoch-60, ts_epoch+60)  ← boundary tolerance
  │   filter: file must exist on disk
  │   empty → HTTP 404
  │
  ├─ pick segment: first where start_epoch ≤ ts_epoch ≤ end_epoch
  │               else segments[0]
  │   seek_offset = max(0.0, ts_epoch - segment.start_epoch)
  │
  ├─ quality mapping: ffmpeg_q = round(31 - (quality/95) × 30)
  │   quality=95 → ffmpeg_q=1 (best), quality=1 → ffmpeg_q=31 (worst)
  │
  ├─ snap_path = clips_path / f"{camera}_{int(ts_epoch)}_{uuid4().hex[:8]}.jpg"
  │
  ├─ ffmpeg -i {segment.filepath}
  │         -ss {seek_offset:.3f}
  │         -frames:v 1
  │         -q:v {ffmpeg_q}
  │         -y {snap_path}
  │   timeout: 15s
  │
  ├─ returncode != 0 → HTTP 500 + stderr
  │
  ├─ background_tasks.add_task(snap_path.unlink, missing_ok=True)
  │   (file is deleted AFTER the response is sent — no clip_ttl needed)
  │
  └─ FileResponse(snap_path, media_type="image/jpeg")
```

---

## 10. SQLite Schema and Index Design

```sql
CREATE TABLE segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    camera      TEXT    NOT NULL,
    start_epoch REAL    NOT NULL,   -- Unix timestamp, wall clock (UTC)
    duration    REAL    NOT NULL,   -- seconds (from PTS or ffprobe)
    filepath    TEXT    NOT NULL UNIQUE,
    file_size   INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL DEFAULT (strftime('%s', 'now'))
);

-- Used by find_segments: WHERE camera=? AND start_epoch < ? AND (start_epoch+duration) > ?
CREATE INDEX idx_camera_start ON segments (camera, start_epoch);

-- Used by delete_before: WHERE camera=? AND (start_epoch+duration) < ?
CREATE INDEX idx_camera_end   ON segments (camera, start_epoch + duration);
```

**Connection settings per thread:**
```
PRAGMA journal_mode = WAL        -- readers don't block writers
PRAGMA synchronous  = NORMAL     -- fsync on checkpoint, not every commit
row_factory = sqlite3.Row        -- column access by name
```

**Concurrency model:**

```
uvicorn thread  ──── read  ─────┐
uvicorn thread  ──── read  ─────┤──▶ SQLite WAL
rec-entrance    ──── write ─────┤    (concurrent reads OK while writer active)
rec-cam01       ──── write ─────┘
retention       ──── write (delete, hourly, brief)
```

---

## 11. Segment Filesystem Layout

```
/data/nvr/
├── segments.db           SQLite WAL index
├── segments.db-wal       WAL journal (normal during writes)
├── segments.db-shm       shared memory for WAL
│
├── entrance/
│   ├── entrance_20260317_143200.ts   (60s segment, ~30 MB at 4 Mbps)
│   ├── entrance_20260317_143300.ts
│   ├── ...
│   └── segments.csv      ffmpeg's output: filename,start_pts,end_pts
│
├── office-cam01/
│   ├── office-cam01_20260317_143200.ts
│   └── segments.csv
│
└── clips/
    ├── entrance_1742221930_a3f9c1b2.mp4   (extracted, deleted after 30 min)
    └── entrance_1742221930_b4e8d2a1.jpg   (snapshot, deleted after response)
```

---

## 12. Docker Deployment Architecture

```
Host
├── docker compose up
│     └── Service: nvr
│           Image: python:3.12-slim + ffmpeg (apt)
│           Port:  8009:8009  (host:container)
│           Env:   TZ=UTC
│           │
│           ├── Volume: nvr-data → /data/nvr  (named volume, persists restarts)
│           ├── Bind:   ./config → /app/config  (read-only)
│           └── Bind:   ./ui     → /app/ui      (read-only)
│
│           Healthcheck (every 30s, 3 retries, 5s timeout):
│             CMD python -c "import urllib.request,sys;
│                            sys.exit(0 if urllib.request.urlopen(
│                              'http://localhost:8009/health',
│                              timeout=4).status == 200 else 1)"
│             Note: python:3.12-slim has no curl — stdlib urllib is used.
│
└── Network (bridge by default)
      If cameras are on the host LAN and bridge NAT blocks RTSP:
      → uncomment network_mode: host in docker-compose.yml
```

---

## 13. End-to-End Timing Profile

```
Event: Person detected by AI pipeline at T=0
          ↓
T+0.0s  AI pipeline calls GET /clip?camera=entrance&timestamp=T&before=10&after=10
          ↓
T+0.0s  FastAPI receives request (async, no blocking)
T+0.0s  DB query: find_segments(T-10, T+10)  [sub-millisecond, indexed]
          ↓
T+0.0s  _extract_clip() called:
          1 or 2 segments to remux (20s window → ≤2 segments at 60s each)
          ffmpeg -c copy (no transcode) reads ~4 MB of TS data
          → produces ~4 MB MP4 in < 1 second on SSD
          ↓
T+0.8s  FileResponse begins streaming MP4 to client
          (moov atom at front due to -movflags +faststart)
          ↓
T+1.5s  Client has full 20-second MP4 clip

Segment recording latency:
  Camera captures frame
    → ffmpeg receives over RTSP (< 100ms LAN)
    → ffmpeg writes to .ts segment (< 5ms SSD)
    → segment completes after 60s
    → CSV entry written by ffmpeg
    → worker polls CSV (up to 2s delay)
    → INSERT into SQLite (< 1ms)
  Total: segment available in index within 2s of it completing on disk
```

---

## 14. Known Failure Modes and Mitigations

| Failure | Detection | Mitigation |
|---------|-----------|------------|
| Camera RTSP drops | ffmpeg exits | Exponential backoff reconnect (5s→60s cap) |
| ffmpeg stalls (packets stop, process alive) | `-timeout 5000000` kills hung socket within 5s → ffmpeg exits | Same reconnect path |
| Indexer stalls (ffmpeg alive, CSV not growing) | Health monitor staleness alarm (lag > 3×60s=180s) | Reconciliation self-heals within 10 min |
| CSV truncated on reconnect | — (handled by design) | Filename watermark; line-count watermark was the original bug |
| Segment file not yet visible when CSV written | `filepath.exists()` → False | Watermark not advanced; retried on next 2s poll tick |
| NVR process killed | — | TS files on disk are valid; `--rebuild-index` on restart |
| SQLite index lost | — | `--rebuild-index`: rglob *.ts + ffprobe each |
| Clip request during recording gap | `find_segments` returns partial list | Returns best-effort clip with `X-NVR-Coverage < 1.0` header |
| Disk full | ffmpeg write fails → exits | Reconnect loop; monitor `/storage` endpoint |
| Docker healthcheck always fails | `curl` not in slim image | Fixed: uses `python -c "urllib.request.urlopen(...)"` |

---

## 15. Sequence Diagram: Segment Lifecycle

```
ffmpeg                segments.csv         StreamWorker         SQLite (WAL)
  │                        │                    │                    │
  │── write packet ──▶ ts  │                    │                    │
  │   (every ~33ms)        │                    │                    │
  │                        │                    │                    │
  │── 60s elapsed ─────────│                    │                    │
  │   close segment        │                    │                    │
  │── append CSV row ──▶   │                    │                    │
  │   "entrance_..ts,      │                    │                    │
  │    0.0,60.003"         │                    │                    │
  │                        │                    │                    │
  │                        │  ◀── poll (2s) ────│                    │
  │                        │── readlines() ────▶│                    │
  │                        │                    │── parse line        │
  │                        │                    │── filepath.exists() │
  │                        │                    │── parse ts from name│
  │                        │                    │── duration from PTS │
  │                        │                    │── INSERT OR REPLACE▶│
  │                        │                    │◀── commit ──────────│
  │                        │                    │── advance watermark │
  │                        │                    │── update metrics    │
  │                        │                    │── reset backoff     │
```
