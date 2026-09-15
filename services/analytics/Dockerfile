# Variphi analytics — detection, plates, tracking.
#
# NO CLIP HERE, and that is the point of the split: this image carries the
# detector and the plate models, Smart Search carries the encoder. Neither
# service pays for the other's weights.
#
# TORCH VARIANT IS A BUILD ARG, and the default is CPU on purpose — the same
# contract Smart Search's image keeps.
#
#   cpu    (default)  runs anywhere, including the appliances this product
#                     actually ships to. OpenVINO is the CPU detector backend
#                     and is measurably faster than torch there anyway.
#   cu121             build it only where there is a GPU, and layer
#                     docker-compose.gpu-cdi.yml so the container can see it.
#
#   docker compose --profile analytics build \
#     --build-arg TORCH_VARIANT=cu121 analytics
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
ARG TORCH_VARIANT=cpu

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# Ultralytics writes settings and telemetry state somewhere writable; point it
# inside the image rather than letting it pick $HOME at runtime.
ENV YOLO_CONFIG_DIR=/app/.ultralytics
# Model weights are NOT baked in: the packaging harness fails on any binary it
# cannot account for, and a .pt file is exactly that. They download on first
# run under $HOME, which is the models volume — shared with Smart Search, so
# the detector is fetched once rather than once per service.
ENV HOME=/models/home

# opencv-python-headless + pillow runtime deps. No libgl: headless needs none,
# and the uninstall below is what keeps the GUI build from arriving anyway.
RUN apt-get update && \
    apt-get install -y --no-install-recommends libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# Torch first and explicitly, so the variant is chosen HERE rather than being
# whatever the transitive resolution of ultralytics happens to pull. Skipping
# this is not a style detail: it produced a 3.52 GB analytics image, because
# the default PyPI torch bundles CUDA libraries this CPU service never runs.
#
# `set -e` AND `;` RATHER THAN `&&` — the same fix Smart Search's image carries.
# A chain ending `... -r requirements.txt && pip uninstall ... || true && pip
# install headless` groups as `((A && B) || true) && C`, so a FAILED
# requirements install is swallowed by the guard written for the uninstall and
# the build exits 0. That shipped once: an image with torch and OpenCV and
# nothing else, reporting success and failing only at startup.
#
# --retries/--timeout because these wheels are large and pip's 15 s default
# read timeout is not generous enough for a constrained link. A slow connection
# should make a build slow, never broken.
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
    # "libxcb.so.1: cannot open shared object file". Both provide the same
    # `cv2` module, so the fix is to put headless back last rather than to
    # install X libraries into a service that never opens a window. This one
    # command is allowed to fail: the packages may simply not be present.
    { pip uninstall -y opencv-python opencv-contrib-python || true; }; \
    pip install --no-cache-dir --retries "${PIP_RETRIES}" --timeout "${PIP_TIMEOUT}" \
      --force-reinstall "opencv-python-headless>=4.9,<4.12"

# A BUILD THAT CANNOT IMPORT ITS OWN DEPENDENCIES IS NOT A BUILD. Cheap, and it
# turns the failure above into one that happens at `docker build` rather than
# on an appliance at 3am. openvino is checked too: it is the default detector
# backend on CPU, and silently shipping without it downgrades every CPU
# deployment to the slower torch path with nothing to show why.
RUN python -c "import fastapi, uvicorn, cv2, numpy, PIL, yaml, redis, \
torch, ultralytics, openvino, onnx, fast_plate_ocr; \
print('image dependency check: ok')"

# AND THE ENCODER MUST NOT BE HERE. If open_clip ever resolves in this image
# the split has quietly undone itself and both services are carrying both model
# sets again — which is the cost this service was created to remove.
RUN python -c "import importlib.util, sys; \
sys.exit(1) if importlib.util.find_spec('open_clip') else print('seam check: no CLIP in this image')"

COPY . .
EXPOSE 8015
CMD ["python", "main.py", "--config", "config.yaml"]
