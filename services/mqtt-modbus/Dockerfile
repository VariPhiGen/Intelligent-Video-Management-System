# Variphi mqtt-modbus translator.
#
# ONE COIL. This is deliberately the smallest image in the stack: an MQTT
# client and pyserial. It runs beside the VMS and owns the RS-485 bus so that
# the VMS itself never has to.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --retries 10 --timeout 120 -r requirements.txt

# A build that cannot import its own dependencies is not a build.
RUN python -c "import aiomqtt, serial; print('image dependency check: ok')"

COPY translator.py .
# A serial device is root-owned on the host and mapped in with the same
# permissions; `dialout` is the group that owns it on Debian/Ubuntu.
RUN useradd --system --uid 10001 --no-create-home app && usermod -aG dialout app
USER app
CMD ["python", "translator.py"]
