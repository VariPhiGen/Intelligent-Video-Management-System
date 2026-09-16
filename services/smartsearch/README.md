# smartsearch — the forensic search index

Person and vehicle crops, embedded and searchable by description. Replaces the
DeepStream → Kafka → Milvus pipeline for **search only**; live activity
analytics stay with DeepStream.

**Status: phase 4.** All of it: database, schema, lifecycle, ingest pipeline and
the six endpoints the VMS speaks — verified in the real SPA, not with curl.

Plate reading **works out of the box**: an open plate detector
(open-image-models, MIT) auto-downloads on first use; site-tuned weights via
`SEARCH_PLATE_WEIGHTS` beat it whenever set — see below.

`/health` reports this under `capabilities` — all three true — and `ingest`
carries live counters (`frames_dropped`, `crops_deduped`, `rows_written`,
`write_failures`). The six query endpoints are **not stubbed**:
`smartsearch_client` treats a 200 as an answer, so a stub returning
`{"results": []}` would be indistinguishable from a working index that found
nothing. Absent means 404, and 404 is honest.

## Layout

```
main.py                              entry point; zero cameras at boot
api/server.py                        /health + camera registry endpoints
index/config.py                      YAML + environment
index/engine.py                      the camera set (in-memory, self-healing)
index/detector.py                    Detector interface + the ONE ultralytics import
index/embedder.py                    one CLIP model, batched, shared
index/dedup.py                       appearance clustering (why, not a tracker)
index/sampler.py                     per-camera capture thread, up to N fps
index/pipeline.py                    the single inference worker + backpressure
index/writer.py                      crop JPEGs on disk
index/store.py                       reachability, schema state, reads + writes
index/motion.py                      the gate in front of the detector
index/plates.py                      two-stage plate reading (read the top of it)
index/queries.py                     the six endpoints' data access
migrations/001_init.sql              tables, HNSW, trigram, retention indexes
migrations/002_hnsw_iterative_scan.sql  filtered-search correctness (read it)
migrations/migrate.py                ordered SQL runner, idempotent
scripts/retention.py                 expire rows + unlink crops
scripts/purge_unrecorded.py          drop rows whose footage no longer exists
```

## Lifecycle

Starts with **zero cameras**. `camera-mgmt` pushes them at runtime
(`POST /cameras/{slug}?rtsp_url=...`) and re-asserts the set every
`nvr_sync_interval` seconds from `backend/services/smartsearch_sync.py`. State
is in-memory, so a restart here self-heals within one interval — the same
lifecycle as the NVR and motion services.

The service never sees camera credentials: it is given relay URLs
(`rtsp://<host>:8654/<slug>`), which is also why indexing adds no load to the
physical camera.

`SMARTSEARCH_INDEX_URL` on the camera-mgmt side selects the index this VMS
**feeds**. It is deliberately separate from `SMARTSEARCH_API_URL`, the index it
**queries** — a VMS can query a remote index without feeding one. Empty means
this VMS feeds no index, and the sync loop never starts.

## The models are loaded on demand and released when idle

The detector, the encoder and the plate reader load when the **first camera is
registered** and are released once the **last one has been gone** for
`SEARCH_IDLE_TIMEOUT` (default 300 s). Switching Smart Search off on every
camera in the UI therefore switches off the work: `camera-mgmt` withdraws the
cameras on its next reconcile, and the weights go a few minutes later. Turning
one back on reloads them in about two seconds. `index/models.py` owns this;
nothing else constructs a model.

Measured end-to-end on the reference appliance, 7 cameras, CPU build:

| | CPU | RSS |
|---|---|---|
| booted, no cameras yet | ~0% | **52 MB** |
| indexing 7 cameras | ~60% | 2.07 GB |
| every camera's Smart Search switched off | **~0.1%** | 1.10 GB |

**The CPU saving is total; the memory saving is partial, and the floor is
one-way.** Importing torch, ultralytics, onnxruntime and OpenCV maps libraries
and arenas that live as long as the process, so a service that has loaded once
never returns to its 52 MB boot figure. If a deployment needs that memory back,
the only thing that gets it is restarting the container. This is the deliberate
trade for keeping **search of already-indexed footage working while nothing is
being indexed** — an archive does not stop being searchable because indexing
was turned off, and the encoder reloads on demand to answer a query.

Three things here are easy to get wrong, and each cost a measurable amount:

* **Release is on a timer, not on the transition.** `upsert_camera` is a remove
  followed by an add, so editing a camera's URL takes a one-camera deployment
  through zero registered cameras. Anything below ~60 s starts unloading and
  reloading on ordinary registry traffic. `SEARCH_IDLE_TIMEOUT=0` disables
  hibernation entirely, which is right for a permanently busy site.
* **Reclaiming while the models are still referenced does nothing, silently.**
  `_teardown` is handed the pipeline, so its caller still holds it — and the
  pipeline holds all three models. Collecting from inside `_teardown` freed
  nothing while every log line and state said "released". It was worth 170 MB.
  `gc.collect()` alone is also not enough: glibc keeps the freed arenas, so
  `malloc_trim(0)` follows it, and `torch.cuda.empty_cache()` follows that or a
  GPU box shows no change in `nvidia-smi` at all.
* **`/health` separates "will this happen" from "is it happening".**
  `capabilities.ingest` stays true while hibernating — a quiet deployment is
  not a broken one — and `lifecycle.ingest.state` carries the truth
  (`disabled` / `idle` / `warming` / `ready` / `failed` / `unavailable`). A
  registered camera with no weights yet reports `WARMING_UP`, never `SAMPLING`
  and never an error. `lifecycle.lifetime` carries the counters forward across
  hibernations so rows written never appears to fall back to zero.

Tests: `python3 -m pytest tests -q` from this directory (16 tests, no models
required — they fake the weights and exercise the state machine and the lock
order).

## Which backend is inference actually running on?

`/health` answers this now, under `lifecycle.inference`. It used to not: the
service reported ingest `ready` without saying whether it was ready on a CPU or
a GPU, and the resolved device existed only in one boot log line. Diagnosing
"why is this slow" should not require reading logs.

Each of the four components reports its own line, because backend selection is
**per model** — a valid configuration can run the detector on one device and the
plate reader on another:

```json
"detector": {
  "representation": "torch", "runtime": "ultralytics", "device": "cuda",
  "selected_by": "incumbent", "artifact": "yolov8n.pt", "loaded": true
}
```

Three fields are worth reading carefully:

- **`representation` / `runtime` / `device` are three separate facts.** ONNX is
  not a device and CUDA is not a representation; an ONNX graph can be executed
  by onnxruntime *or* by OpenVINO. Collapsing them is how a system acquires a
  rule like "a GPU exists, therefore everything runs on it".
- **`selected_by`** distinguishes a decision from a default. `incumbent` means
  what shipped, and that nothing else has been proven loadable here yet;
  `sole-candidate` means there is genuinely only one implementation (the
  encoder — see the note at the top of `index/embedder.py`).
- **`loaded`** is separate from the rest, because the rest **survives
  hibernation**. An operator checking a sleeping appliance still needs to know
  which device inference uses; "the models are released so we cannot tell you"
  answers a different question.

`GET /diagnostics/hardware` answers the other half — not which device was
picked, but which devices and runtimes were there to pick from — plus the
environment fingerprint. It is deliberately not part of `/health`: the container
healthcheck runs every 30 s with a 5 s timeout, and probing runtimes and reading
CPU flags belongs nowhere near that path.

**The CPU count is container-aware.** `os.cpu_count()` reports the host's
processors, so a container limited to two cores on a 32-core host reports 32.
`usable_cpus` is the smallest of the host count, the scheduler affinity mask and
the cgroup quota, and `constrained` says when they differ.

### Detection input is letterboxed square, and that is a change

`models.square_letterbox` defaults to **true**, which disables ultralytics'
rectangular padding. It exists so that choosing a backend is a performance
decision and nothing else.

Measured 2026-09-04. An exported artifact — ONNX or OpenVINO — has a **fixed
640×640 input** and must pad square. The torch path pads only to a stride
multiple, so an 1080×810 frame becomes 640×480. The two regimes see different
images and find different objects:

| Backend | bus.jpg | wide 405×1080 |
|---|---|---|
| torch, rectangular *(the old default)* | 4 detections | 2 |
| torch, square · ONNX · OpenVINO | **5** | **3** |

The extra detection is a partially-visible person at the frame edge, at 0.436.
With square letterboxing, torch and ONNX agree **exactly**; OpenVINO differs
only in kernel-level rounding (±0.014 confidence, same objects).

Neither `imgsz=640` nor `imgsz=(640,640)` achieves this — only disabling
rectangular inference does. Exported models ignore the setting entirely,
because their input shape has already decided it.

**Two costs, both real.** A square letterbox processes ~33% more pixels for a
4:3 frame, and the extra marginal detections become extra crops, embeddings and
stored rows — so expect the storage figures above to shift. Set it to `false`
to keep the historic behaviour, at the price of no longer being able to swap
backends without changing what is detected.

**Sites running ANPR should re-validate.** The two-line plate thresholds were
fitted to 109 real reads taken under rectangular preprocessing, and square
padding changes which plate regions are boxed and how much margin they carry —
margin being exactly what the recogniser needs.

### Probing an image

```bash
python3 scripts/probe.py             # what this image can run inference on
python3 scripts/probe.py --json      # machine-readable
python3 scripts/probe.py --quick     # skip export and model construction
```

The deep checks answer three questions that decide which alternative backends
are real, and that **cannot be answered anywhere but inside the deployed
image**: what device and provider values the installed `fast_plate_ocr` accepts,
whether an ultralytics ONNX/OpenVINO export preserves `model.names` — which
`UltralyticsPlateLocaliser` needs for its plate-class filter — and whether two
`onnxruntime` distributions can coexist. The answers differ between the CPU
image and the `cu121` image, so run it in both. A missing package is reported as
a finding, not an error.

## Running it

The database is opt-in and separate from the camera registry:

```bash
docker compose --profile search up -d searchdb smartsearch
SEARCHDB_URL=postgresql://search:search_secret@127.0.0.1:5434/smartsearch \
  python3 migrations/migrate.py
```

`--status` lists what is applied and what is pending. Re-running is a no-op.

Retention runs from `scripts/retention.py` (`--dry-run` to count only,
`--sweep-orphans` to reclaim crop files no row references).

**Retention is per camera, and it follows the camera's RECORDING retention.**
`camera-mgmt` resolves each camera's policy to a concrete number of days and
pushes it with the registration (`POST /cameras/{slug}?retention_days=`), which
stamps new rows and RESTAMPS the ones already indexed for that camera. Resolved
upstream on purpose: "default" means `nvr_default_retention_days` to the NVR and
`SEARCH_RETENTION_DAYS` to this service, and nothing keeps those two in step —
an appliance dropped to two days of footage would otherwise keep thirty days of
crops cut from it. `SEARCH_RETENTION_DAYS` remains the fallback for a camera the
registry has not spoken for.

**Age is not the only way the index outlives its footage.** The recorder and the
indexer are separate services with separate lifecycles, so a restart skew, a
camera re-added under a new slug, or an NVR erasure that did not reach here
leaves detections standing over dead air — hits whose playback 404s. The
retention thread reconciles the two on its own timer (first pass after start,
then every `SEARCH_RETENTION_COVERAGE_EVERY_N`th sweep — daily by default): it
asks the NVR `GET /coverage` what it still holds and erases the rows outside it.
`scripts/purge_unrecorded.py` runs the same code on demand.

Three bounds make that safe to run unattended, and they matter more than the
deletion does:

- **Unknown is not absent.** An unreachable or 404ing recorder skips that
  camera and is retried on the next pass, rather than reading silence as
  permission to erase the camera whole. An empty `NVR_URL` disables the sweep
  for the same reason.
- **Recent rows are never judged.** The in-progress segment is not in
  `segments.db` yet, so `latest` lags wall-clock; without the hour-wide grace
  an hourly sweep would delete the newest detections on every pass, for footage
  being recorded right now.
- **A pass that would take most of a camera refuses itself.** A rebuilt or
  truncated `segments.db` answers successfully and reports almost nothing
  recorded, which is indistinguishable from genuinely lost footage — and only
  one of the two is recoverable. `--force` is the deliberate override, and the
  timer never has it.

## Two things to know before changing anything here

**The embedding dimension is 512 and changing it is not a migration.** Vectors
from a different model are not comparable, so a dimension change means
re-indexing every crop from footage. 512 is OpenCLIP ViT-B/32, chosen because
tier 1 has to run on CPU-only appliances and SigLIP-SO400M does not.

**Filtered vector search is the hot path, and HNSW gets it wrong by default.**
Every query is scoped to a camera set. HNSW post-filters, so with the default
`ef_search` the index can hand back 40 candidates, have every one of them
filtered away, and return zero rows while reporting success — which reads to an
operator exactly like "this person was never recorded". Migration 002 sets
`hnsw.iterative_scan` on the database to prevent it. Do not remove it, and do
not rely on a client setting it per session.

## Measured (2026-08-31, 100,068 rows, RTX 2000 Ada box)

| | |
|---|---|
| Storage, all-in | **5.7 kB per row** (541 MB table + index; the vector itself is only 2 kB) |
| HNSW index alone | 261 MB per 100k rows |
| Insert rate with HNSW live | 391 rows/s |
| Scoped query, top-5 | 0.5–1.6 ms |
| Unscoped top-24 over 100k | 3.5 ms |

## Measured (phase 3, 2026-08-31)

| | |
|---|---|
| End-to-end pipeline, GPU | **128 fps** — 600 frames detect+embed+dedup+write in 4.7 s |
| Dedup on real footage | **68 detections → 8 rows**, matching a 15 FPS tracker's identity count |
| CPU-only container, 4 cameras @ 1 fps | **0.65 of a core**, 1.9 GB RSS |
| Frames dropped under load | 0 of 1,057 across two live runs |
| Image size | 2.4 GB CPU build; the `cu121` build is far larger |

The CPU figure is a **floor**: no people were present during the live run, so
the embedder never ran. Detection is the only cost it includes.

## Per-camera domains

`cameras.search_domains` (migrations 031 and 035) picks which of `person` /
`vehicles` / `plate` / `face` a camera contributes. Pushed with the camera and
honoured per frame. `face` is off by default; the first camera that asks for it
triggers the model fetch — see "Face models fetch themselves" below.

**It does not save detection time.** Measured: 120 frames took 1.12 s for
person+vehicles, 1.09 s person-only, 1.08 s unfiltered — the detector's class
filter runs after the forward pass. Narrowing saves **storage** (the binding
constraint), **plate reading** (a second model per vehicle crop), and embedding
of crops that would have been excluded. The CPU saving comes from the motion
gate, not from here.

Registration is an **upsert**, deliberately: the registry reconciles from four
uvicorn workers concurrently, and a create-or-409 plus remove-and-re-add pair
raced into a camera that was registered but not sampled.

## The motion gate

Inline, on the frame already decoded — asking `vms_motion` instead would decode
every stream twice, costing more than the inference it saves. Frigate does its
motion detection inline for the same reason.

**The gate is the saving; cropping to regions is not.** The detector resizes any
input to its own resolution, so three crops cost three inferences where one
frame costs one. Regions buy accuracy on distant figures — a far person survives
a crop and is lost in a full-frame downscale — so they are used only while they
stay few and small, and the frame is used whole beyond that.

Measured 2026-08-31, one 10-minute clip, gate off vs on:

| | Off | On |
|---|---|---|
| Rows written | 8 | **8** |
| Detector calls per frame | 1.0 | **0.118** |
| Wall clock | 9.0 s | **2.6 s** |
| Frames skipped | — | 89% |

Live on five appliance cameras: 471 frames, **7 detector calls**, CPU 41% → 20%
of a core.

**The thresholds were fitted to eight people.** At the original 25 / 0.0008 the
gate found 7 where ungated found 8 — it lost someone. 18 / 0.0002 recovers all
8. Going further *hurt*: 15 / 0.0001 returned 6, because the gate changes which
frames reach the deduplicator and more crops make it merge two people into one.
Sensitivity and recall are **not monotonic**, so do not tune up "to be safe".
Re-validate at a real site; `SEARCH_MOTION_GATE=false` disables it entirely.

## Plate reading

Two stages, and **the localiser is required**. Recognition models are trained on
tightly cropped plates; handed a whole car they return an empty string.

Measured 2026-08-31 against `cct-xs-v2-global-model`:

| Input | Decoded | Mean char prob |
|---|---|---|
| synthetic plate crop | `H12DE143` | 0.929 |
| random noise | *(empty)* | **1.000** |
| flat grey | *(empty)* | 0.993 |
| a person crop | *(empty)* | **1.000** |

**Confidence does not separate plates from non-plates** — a non-plate scores
1.000, because the model is confidently predicting "no characters". The usable
signal is the emptiness of the decode (`min_length`); confidence only grades how
well a real plate was read. Anything thresholding on confidence alone accepts
every piece of noise it is given.

So the localiser is required — and it has two sources, which is the product
shape (the Frigate model: open baseline, paid accuracy):

* **Open baseline** (`plates.localiser_model`, default
  `yolo-v9-t-384-license-plate-end2end`): an open-image-models detector — MIT,
  ONNX, same author as the recogniser — auto-downloaded on first use like every
  other model here. This is why plate reading works on a clean install.
  Generic global training: it finds plates; a region's formats deserve better.
* **Site-tuned override** (`SEARCH_PLATE_WEIGHTS`): any plate-detection model,
  including an existing ANPR model since the localiser is class-agnostic,
  loaded through ultralytics. Wins over the baseline whenever set — this is
  where a region-trained (or commercial) model plugs in. A failed load does
  NOT fall back to the baseline: silently substituting a generic model would
  make "misconfigured" look like "mediocre".

Only with **both** empty (`SEARCH_PLATE_MODEL=` explicitly) is plate reading
inactive, and `/health` reports `plates.active: false` rather than running
recognition over whole cars and storing nothing.

**Crops need margin.** The same three plates read as `L8CAF503` / `AO5MJ456` /
`H12DE143` from crops hugging the characters and **exactly** from renders with
real margin. `region_padding` adds some back, but it can only include pixels
that exist — a crop already flush to the plate edge stays truncated. The
localiser must box generously.

Licences differ between the stages on purpose: recognition is `fast-plate-ocr`
(**MIT**, ONNX, ~3 MB), localisation goes through ultralytics (**AGPL**). The
half that would be hardest to replace is the half with no licence entanglement.

**Accuracy here is unvalidated.** This appliance has no vehicles at all
(`search_vehicles` was empty, and the cameras are indoor), so every test above
used synthetic images. They prove the plumbing — localise, recognise, normalise,
store, trigram match — and nothing about real-world reads.

## The contract is six endpoints, not four

Four are called through `smartsearch_client`. Two — `/{domain}/image` and
`/detections` — are reached directly by `routers/search.py`, so they are
invisible if you only read the client. Omitting them does not fail loudly: you
get a results grid with no thumbnails and a dashboard that 502s.

Three rules, all learned expensively by the service this replaces:

- **`scoped: true`** on stats and detections or the VMS refuses the response.
- **Filter on the camera id, never a name.** The old service resolved names
  against its own table and silently ran the query *unfiltered* when it did not
  recognise one.
- **Timestamps:** ISO-8601 on search hits, epoch **milliseconds** on
  `/detections` — the dashboard does `new Date(ts)`.

When the encoder is unavailable the search routes answer **503**, never an empty
result set. "Nothing matched" is the one answer a search must never give wrongly.

### `/detections` carries the plates, not just a count of them

`plates_read` alone answers "is the reader running". It does not answer "who
came through", and until the fields below existed the only way to see a plate on
this deployment was to already know it and use the ANPR lookup. The response now
also carries:

| field | what it is |
|---|---|
| `recent_plates[]` | the reads themselves — plate, confidence, camera, time |
| `top_plates[]` | ranked by sightings, with camera count and first/last seen |
| `recent[].plate` | the plate on the vehicle row it was read from, or null |
| `summary.vehicles_seen` | the denominator, so a count becomes a read rate |

Three things about this shape are deliberate:

- **Plates are not a third slice of the detection total.** People and vehicles
  partition it; a plate is an attribute of a vehicle already counted. So the
  dashboard shows plates as COVERAGE of vehicles seen, and `vehicles_seen`
  exists to make that possible. Adding plates to the split bar would
  double-count every vehicle whose plate was read.
- **`recent_plates` is separate from `recent`.** A vehicle appears in the feed
  whether or not its plate could be read; folding the two would make "no plate"
  and "not a vehicle" the same row.
- **`domain=` narrows the FEED ONLY, never the summary.** On a real site people
  outnumber vehicles several times over — measured here, 56 of the newest 60
  detections — so without the filter the vehicle half of the product is
  invisible on its own dashboard. Both domains are already fetched and the merge
  then discards the vehicles, so the filter costs no extra query. The summary
  stays whole, or an operator would read a filtered "1,907 vehicles" as the
  site's entire activity.

Plate confidence needs migration **003**. Before it, `index/pipeline.py` produced
a `plate_confidence` on every row and `store.write_batch` — which names its
columns explicitly — dropped it silently, because there was no column. Rows
written before that migration have NULL, which the UI shows as "—" and never
as 0: "not recorded" and "scored zero" are different facts.

## Model weights are never committed

`ultralytics` downloads `yolov8n.pt` into the working directory on first use.
`.gitignore` and the packaging harness both block it now — the harness gained
model extensions on 2026-08-31 after exactly this file was staged by
`git add -A` and the sweep did not object, because `.pt` was not an extension it
knew about. In the container, weights land in the `search_models` volume.

### Face models fetch themselves

Face search (the `face` camera domain) uses two models from the
[OpenCV Zoo](https://github.com/opencv/opencv_zoo). This repository does not
redistribute them — the open-core packaging harness refuses binaries — so they
are **fetched on first use and verified by sha256**, exactly as the detector
and the plate models are fetched by their own libraries. No manual step.

| File | Model | Licence |
|---|---|---|
| `face_detection_yunet_2023mar.onnx` (232 KB) | YuNet face detector | MIT |
| `face_recognition_sface_2021dec.onnx` (37 MB) | SFace face embedder | Apache-2.0 |

The fetch happens in the warm-up that loads the face reader — the first time a
camera asks for the `face` domain — and lands in the shared `search_models`
volume, which both services mount at `/models`. Whichever service warms up
first pays for it; the other finds the files already there.

**If the fetch cannot happen** — no egress, a proxy, a digest that no longer
matches upstream — nothing is installed and both services behave exactly as
they did before: they start normally, `/health` shows
`lifecycle.faces.available: false`, analytics logs `face models not available`,
and people, vehicles and plates are unaffected. A file that fails verification
is discarded rather than left on disk, so a truncated download or a captive
portal's login page can never be loaded as a model.

`ANALYTICS_FACE_AUTO_DOWNLOAD=false` / `SEARCH_FACE_AUTO_DOWNLOAD=false` turns
the fetch off — for an air-gapped site that stages the files itself, which is
the manual path, still supported and unchanged:

```sh
base=https://github.com/opencv/opencv_zoo/raw/main/models
curl -fL -o face_detection_yunet_2023mar.onnx   "$base/face_detection_yunet/face_detection_yunet_2023mar.onnx"
curl -fL -o face_recognition_sface_2021dec.onnx "$base/face_recognition_sface/face_recognition_sface_2021dec.onnx"
sha256sum -c <<'EOF'
8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4  face_detection_yunet_2023mar.onnx
0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79  face_recognition_sface_2021dec.onnx
EOF
docker cp face_detection_yunet_2023mar.onnx   vms_smartsearch:/models/
docker cp face_recognition_sface_2021dec.onnx vms_smartsearch:/models/
docker restart vms_analytics vms_smartsearch
```

Then switch `face` on per camera in AI Config. It is off by default on every
camera (migration 035): faces are biometric data, and most cameras rarely see
one large enough to search. **The checksums matter.** The index stores the
embedding model's name on every row and refuses a mismatched one, so a
different SFace build would silently rank against vectors it does not share a
space with.
