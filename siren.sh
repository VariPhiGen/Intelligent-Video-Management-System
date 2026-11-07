#!/usr/bin/env bash
# Fire the siren relay for N seconds (default 5).
#
#   ./siren.sh        5 seconds
#   ./siren.sh 15     15 seconds
#   ./siren.sh on     latch on  (the device's own 60s watchdog still applies)
#   ./siren.sh off    latch off
#   ./siren.sh status what the board last reported
#
# Talks to the VMS's own broker. Credentials come from .env, so nothing is
# hardcoded here and the password never appears on a command line.
set -euo pipefail
cd "$(dirname "$0")"

PW=$(grep '^MQTT_PASSWORD=' .env | cut -d= -f2-)
TOPIC=vms/siren/site-siren/set
STATE=vms/siren/site-siren/state
AVAIL=vms/siren/site-siren/availability

m () { docker run --rm --network host eclipse-mosquitto:2 "$@" 2>/dev/null; }
pub () { m mosquitto_pub -h 127.0.0.1 -u vms -P "$PW" -t "$TOPIC" -m "$1"; }
sub () { m mosquitto_sub -h 127.0.0.1 -u vms -P "$PW" -t "$1" -C 1 -W 4; }

case "${1:-5}" in
  on)     pub on;  echo "ON (watchdog will drop it after 60s)" ;;
  off)    pub off; echo "OFF" ;;
  status) echo "availability: $(sub $AVAIL)"; echo "relay       : $(sub $STATE)" ;;
  *)
    SECS="${1:-5}"
    echo "board: $(sub $AVAIL)"
    pub on
    for i in $(seq 1 "$SECS"); do printf "\r  ON %2ss " "$i"; sleep 1; done
    pub off
    printf "\r  done — relay off%*s\n" 10 ""
    ;;
esac
