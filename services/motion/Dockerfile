FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
# CPU-only by contract (opencv built with CUDA must not grab a GPU), plus the
# usual Python container hygiene.
ENV CUDA_VISIBLE_DEVICES="" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# opencv-python-headless runtime deps (no libgl needed for headless).
RUN apt-get update && \
    apt-get install -y --no-install-recommends libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8012

ENTRYPOINT ["python", "main.py"]
CMD ["--config", "config.yaml"]
