# Variphi frame broker.
#
# DELIBERATELY SMALL. This service decodes and differences; it runs no models,
# so it carries none of smartsearch's ~2.5 GB of torch and weights. Keeping it
# light is what makes it reasonable to put in front of two other services.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# libGL and libglib are OpenCV's runtime deps even in the headless build.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --retries 10 --timeout 120 -r requirements.txt

# A build that cannot import its own dependencies is not a build.
RUN python -c "import fastapi, uvicorn, cv2, numpy, yaml, redis; \
print('image dependency check: ok')"

COPY . .
EXPOSE 8014
CMD ["python", "main.py", "--config", "config.yaml"]
