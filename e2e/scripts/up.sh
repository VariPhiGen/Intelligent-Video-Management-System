#!/usr/bin/env bash
# up.sh — bring up the E2E stack and wait until it is genuinely ready.
#
# THREE OVERLAYS, IN ORDER, and the order is the meaning:
#   docker-compose.yml            the product, as it ships
#   docker-compose.bridge.yml     off host networking, onto an isolated bridge
#   e2e/docker-compose.e2e.yml    the simulators, and E2E-only settings
#
# The product file is never edited for testing. Everything the harness needs is
# additive, so a service that behaves differently under E2E is visible as a line
# in one small file rather than a flag buried in the real one.
#
# PROJECT NAME IS THE ISOLATION. `-p vms-e2e` gives this stack its own
# containers, network and volumes; a developer's own stack (default project
# name) is untouched, and `down.sh -v` cannot reach it.
#
# READINESS IS A CONDITION, NEVER A SLEEP. The script waits for each service to
# answer, with a deadline, and prints what it is waiting for. A fixed sleep here
# would be the first flake in the suite and would teach everyone after us that
# sleeps are how you fix flakes.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../.." && pwd)"
ENV_FILE="${HERE}/../e2e.env"
PROJECT="${E2E_PROJECT:-vms-e2e}"

cd "${ROOT}"

compose() {
  docker compose \
    --project-name "${PROJECT}" \
    --env-file "${ENV_FILE}" \
    -f docker-compose.yml \
    -f docker-compose.bridge.yml \
    -f e2e/docker-compose.e2e.yml \
    "$@"
}

# Read a value out of the env file so the script and the stack cannot disagree
# about which port to poll.
env_val() { sed -n "s/^${1}=//p" "${ENV_FILE}" | head -1; }

API_PORT="$(env_val API_PORT)"
KEYCLOAK_PORT="$(env_val KEYCLOAK_PORT)"

echo "▸ project=${PROJECT}  api=:${API_PORT}  keycloak=:${KEYCLOAK_PORT}"

if [ "${E2E_FRESH:-1}" = "1" ]; then
  echo "▸ removing any previous E2E stack and its volumes"
  compose down -v --remove-orphans >/dev/null 2>&1 || true
fi

echo "▸ building fixture images"
compose build rtsp-cam-1 rtsp-cam-2 onvif-sim

echo "▸ starting the stack"
compose up -d

# ── Readiness ──────────────────────────────────────────────────────────────
# Each gate is the question a test would ask, not a proxy for it.
wait_for() {
  local label="$1" deadline="$2"; shift 2
  local until=$(( SECONDS + deadline ))
  printf '  waiting: %-34s' "${label}"
  while [ "${SECONDS}" -lt "${until}" ]; do
    if "$@" >/dev/null 2>&1; then echo "ok"; return 0; fi
    sleep 1
  done
  echo "TIMEOUT after ${deadline}s"
  echo "--- last 40 lines of the stack log ---" >&2
  compose logs --tail 40 >&2 || true
  return 1
}

http_ok() { curl -fsS --max-time 4 "$1" >/dev/null; }

wait_for "keycloak realm"      180 http_ok "http://localhost:${KEYCLOAK_PORT}/realms/vms/.well-known/openid-configuration"
wait_for "api /health"         180 http_ok "http://localhost:${API_PORT}/health"
wait_for "api serves the SPA"   60 http_ok "http://localhost:${API_PORT}/"
wait_for "auth config is oidc"  60 bash -c \
  "curl -fsS --max-time 4 http://localhost:${API_PORT}/api/auth/config | grep -q '\"mode\":\"oidc\"'"
wait_for "onvif simulator"      60 bash -c \
  "docker exec ${PROJECT}-onvif-sim-1 python3 -c \"import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080').read()\" 2>/dev/null || docker exec vms-e2e-onvif-sim python3 -c \"import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080').read()\""
wait_for "mediamtx control api"        120 bash -c \
  "docker exec vms-e2e-onvif-sim python3 -c \"import urllib.request;urllib.request.urlopen('http://mediamtx:9987/v3/paths/list').read()\""

# THE CAMERAS ARE SERVERS, not publishers into the stack's relay. The product's
# MediaMTX holds only paths the api created, so before any camera is onboarded
# it is legitimately empty — waiting for a path there would hang forever. What
# readiness means here is that each camera answers RTSP on its own address,
# which is exactly what the api will later ask MediaMTX to pull from.
for cam in rtsp-cam-1 rtsp-cam-2; do
  wait_for "camera ${cam} serving RTSP" 120 \
    docker exec "vms-e2e-${cam}" sh -c \
      "ffprobe -v error -rtsp_transport tcp -timeout 3000000 \
         -i rtsp://127.0.0.1:8554/e2e-${cam#rtsp-} -show_entries stream=codec_name -of csv=p=0"
  # ${cam#rtsp-} is expanded HERE, by this script. It was escaped until
  # 2026-09-15, which deferred it to the container's shell, where $cam does not
  # exist: every probe asked for the path `e2e-`, the camera answered "path is
  # not configured", and this gate could only ever time out.
done

echo "▸ stack is ready"
echo "     SPA        http://localhost:${API_PORT}/"
echo "     Keycloak   http://localhost:${KEYCLOAK_PORT}/"
