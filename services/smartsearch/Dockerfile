# Smart Search index service.
#
# TORCH VARIANT IS A BUILD ARG, and the default is CPU on purpose.
#
#   cpu    (default)  ~2.4 GB image (measured). Runs anywhere, including the appliances
#                     this product actually ships to. Phase 0 measured YOLOv8n
#                     at ~117 fps on a GPU; the CPU path is far slower but the
#                     workload is 1 frame per second per camera, so a modest box
#                     still covers a handful of cameras. This is the tier-1
#                     story and it is the thing DeepStream cannot offer at all.
#   cu121             ~6 GB image. Build it only where there is a GPU, and layer
#                     docker-compose.gpu-cdi.yml so the container can see it.
#
#   docker compose --profile search build \
#     --build-arg TORCH_VARIANT=cu121 smartsearch
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
ARG TORCH_VARIANT=cpu

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Ultralytics writes settings and telemetry state somewhere writable; point
    # it inside the image rather than letting it pick $HOME at runtime.
    YOLO_CONFIG_DIR=/app/.ultralytics \
    # Model weights are NOT baked in: the packaging harness fails on any binary
    # it cannot account for, and a .pt file is exactly that. They download on
    # first run into this directory, which is a volume in compose.
    TORCH_HOME=/models \
    HF_HOME=/models/hf \
    # fast-plate-ocr and open-image-models cache their downloads via
    # platformdirs, which honours XDG_CACHE_HOME on Linux — route it into the
    # models volume so the plate models survive a container recreate too.
    XDG_CACHE_HOME=/models/cache \
    # ...except NEITHER library actually consults XDG_CACHE_HOME: both hardcode
    # `pathlib.Path.home() / ".cache" / <name>` (open_image_models/detection/
    # core/hub.py, fast_plate_ocr/inference/hub.py). With HOME still /root that
    # put the plate localiser and the plate OCR outside the volume, so every
    # container recreate re-downloaded them — ~11 MB and a couple of minutes
    # added to warm-up, and on an air-gapped appliance ingest never starts at
    # all. Path.home() honours $HOME on Linux, so this is the lever that works.
    HOME=/models/home

# opencv-python-headless + pillow runtime deps. No libgl: headless needs none.
RUN apt-get update && \
    apt-get install -y --no-install-recommends libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# Torch first and explicitly, so the variant is chosen here rather than being
# whatever the transitive resolution of ultralytics happens to pull.
# `set -e` AND `;` RATHER THAN `&&`, and that is a fix rather than a style
# choice. This chain used to end `... -r requirements.txt && pip uninstall ...
# || true && pip install headless`, which the shell groups as
# `((A && B) || true) && C` — so a FAILED `pip install -r requirements.txt` was
# swallowed by the `|| true` written for the uninstall, and the build exited 0.
#
# OBSERVED 2026-09-04, and it was not a hypothetical: a pip read timeout during
# the download produced a 1.71 GB image with torch and OpenCV and NOTHING from
# requirements.txt — no ultralytics, no onnxruntime, no fastapi — which
# reported a successful build and would only have failed when the service tried
# to start. With `set -e` every command must succeed except the uninstall,
# which keeps its guard explicitly.
#
# --retries/--timeout because these wheels are large (torch alone is ~800 MB)
# and pip's 15 s default read timeout is not generous enough for a constrained
# link. The same day, builds failed with ReadTimeoutError against both
# download.pytorch.org and files.pythonhosted.org on an otherwise working
# network. A slow connection should make a build slow, never broken.
ARG PIP_RETRIES=10
ARG PIP_TIMEOUT=120
RUN set -eu; \
    pip install --no-cache-dir --retries "${PIP_RETRIES}" --timeout "${PIP_TIMEOUT}" \
      --index-url "https://download.pytorch.org/whl/${TORCH_VARIANT}" \
      torch torchvision; \
    pip install --no-cache-dir --retries "${PIP_RETRIES}" --timeout "${PIP_TIMEOUT}" \
      -r requirements.txt; \
    # ultralytics depends on opencv-python (the GUI build), which lands on top
    # of opencv-python-headless and then fails at import with
    # "libxcb.so.1: cannot open shared object file". Both packages provide the
    # same `cv2` module, so the fix is to put headless back last rather than to
    # install X libraries into a service that never opens a window. This one
    # command is allowed to fail: the packages may simply not be present.
    { pip uninstall -y opencv-python opencv-contrib-python || true; }; \
    pip install --no-cache-dir --retries "${PIP_RETRIES}" --timeout "${PIP_TIMEOUT}" \
      --force-reinstall "opencv-python-headless>=4.9,<4.12"

# A BUILD THAT CANNOT IMPORT ITS OWN DEPENDENCIES IS NOT A BUILD. Cheap, and it
# turns the failure above into one that happens at `docker build` rather than
# on an appliance at 3am. `multipart` is checked because POST /observations —
# the only way anything now enters this index — raises at REQUEST time without
# it, so a missing pin looks like a working build and a broken ingest.
RUN python -c "import fastapi, uvicorn, psycopg, cv2, numpy, PIL, yaml, \
torch, open_clip, multipart; \
print('image dependency check: ok')"

# AND THE DETECTOR MUST NOT BE HERE. Detection moved to services/analytics; if
# ultralytics or the plate models ever resolve in this image again, the split
# has quietly undone itself and both services are carrying both model sets.
RUN python -c "import importlib.util as u, sys; \
present = [m for m in ('ultralytics', 'fast_plate_ocr', 'open_image_models') if u.find_spec(m)]; \
sys.exit('seam check FAILED, detector packages present: %s' % present) if present else print('seam check: no detector in this image')"

COPY . .
RUN mkdir -p /models /models/home /models/weights /app/.ultralytics

EXPOSE 8013

ENTRYPOINT ["python", "main.py"]
CMD ["--config", "config.yaml"]
