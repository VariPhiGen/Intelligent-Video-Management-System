# VMS device gateway — standard library only, so the image is just Python.
# 3.12, not later: the multicast speaker uses audioop, removed in 3.13.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY devgw/ devgw/
COPY gateway.py .
RUN python -c "import devgw.server, audioop; print('image check: ok')"

RUN useradd --system --uid 10001 --no-create-home app
USER app
CMD ["python", "gateway.py"]
