#!/usr/bin/env bash
# down.sh — remove the E2E stack. `-v` also removes its volumes.
#
# Volumes are project-scoped, so this cannot reach a developer's own stack even
# with -v. That is the whole reason the project name is pinned.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../.." && pwd)"
PROJECT="${E2E_PROJECT:-vms-e2e}"
cd "${ROOT}"
docker compose \
  --project-name "${PROJECT}" \
  --env-file "${HERE}/../e2e.env" \
  -f docker-compose.yml -f docker-compose.bridge.yml -f e2e/docker-compose.e2e.yml \
  down --remove-orphans "$@"
