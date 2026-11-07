#!/bin/sh
# The VMS broker's entrypoint: render mosquitto.conf and the password file
# from the environment, then hand over to the image's own entrypoint.
#
# mosquitto.conf has no variable substitution, and a password file is a hash
# made by mosquitto_passwd, so neither can be a static file in the repo: the
# port and the credential live in .env like every other secret here.
#
# WHAT THIS BROKER IS. The private bus between the VMS bridge and whatever
# actuates for it — the mqtt-modbus translator, or a Home Assistant on the
# same box. Bound to 127.0.0.1 by default, so nothing off-host can reach it:
# a broker on 0.0.0.0 with anonymous access is a remote switch for every
# device bound to it. A TLS listener for off-host clients is opt-in below.
set -eu

: "${MQTT_USERNAME:?MQTT_USERNAME must be set (see .env.example)}"
: "${MQTT_PASSWORD:?MQTT_PASSWORD must be set (see .env.example)}"
PORT="${MQTT_PORT:-1883}"
BIND="${MQTT_BIND:-127.0.0.1}"

# The password file: written fresh on every start from .env, never persisted,
# owned by the account the broker drops to (1883 in this image) so a SIGHUP
# reload can still read it. REMOVED FIRST: mosquitto_passwd -c refuses to
# overwrite ("File exists"), and the container's filesystem survives a plain
# restart — so without this every reboot put the broker in a crash loop.
# Found by `docker start` on the appliance.
rm -f /mosquitto/passwd
mosquitto_passwd -b -c /mosquitto/passwd "$MQTT_USERNAME" "$MQTT_PASSWORD"
chown 1883:1883 /mosquitto/passwd
chmod 600 /mosquitto/passwd

{
  echo "# Rendered by deploy/mosquitto/entrypoint.sh at start. Edit .env, not this."
  echo "per_listener_settings false"
  echo "allow_anonymous false"
  echo "password_file /mosquitto/passwd"
  echo
  echo "listener $PORT $BIND"
  echo
  # Retained state (the last thing each device reported) and queued QoS 1
  # commands survive a broker restart. Without this the wall comes back
  # `unknown` for every device until it next reports.
  echo "persistence true"
  echo "persistence_location /mosquitto/data/"
  echo "autosave_interval 60"
  echo
  # Commands and state are a few bytes; anything large on this bus is a
  # mistake or an attack.
  echo "message_size_limit 65536"
  echo "max_queued_messages 1000"
  echo "max_inflight_messages 20"
  echo
  echo "log_dest stdout"
  echo "log_type error"
  echo "log_type warning"
  echo "log_type notice"
  echo "connection_messages true"
  # Off-host clients (a Home Assistant on another machine) get a TLS listener
  # when a certificate is provided. Same password file; no anonymous access.
  # UNTESTED against a real certificate — rendered only.
  if [ -n "${MQTT_TLS_PORT:-}" ]; then
    echo
    echo "listener $MQTT_TLS_PORT 0.0.0.0"
    echo "certfile /mosquitto/certs/server.crt"
    echo "keyfile /mosquitto/certs/server.key"
    [ -f /mosquitto/certs/ca.crt ] && echo "cafile /mosquitto/certs/ca.crt"
  fi
} > /mosquitto/config/mosquitto.conf

exec /docker-entrypoint.sh mosquitto -c /mosquitto/config/mosquitto.conf
