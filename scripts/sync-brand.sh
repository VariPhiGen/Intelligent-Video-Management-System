#!/usr/bin/env bash
# sync-brand.sh — propagate the brand logo to every consumer.
#
# The MASTER logos live in  frontend-react/src/assets/  — two theme variants:
#   logo-light.(svg|png)   shown in light mode  (light square, accent mark)
#   logo-dark.(svg|png)    shown in dark mode   (dark square, accent mark)
# The React app references them directly; the Keycloak sign-in theme is a
# separate service and needs its own physical copies, so edit the masters, then
# run this to keep them in sync.
#
#   ./scripts/sync-brand.sh
#
# To switch formats (e.g. SVG → PNG): drop logo-light.png / logo-dark.png beside
# the masters, update the url() extensions in styles/global.css (.brand-mark) and
# the Keycloak vms.css (.v-logo), then run this again.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/frontend-react/src/assets"
DEST="$ROOT/deploy/keycloak/themes/vms/login/resources/img"
mkdir -p "$DEST"

copied=0
for base in logo-light logo-dark; do
  for ext in svg png; do
    master="$SRC/$base.$ext"
    [ -f "$master" ] || continue
    cp "$master" "$DEST/"
    echo "synced $base.$ext → $DEST/"
    copied=$((copied + 1))
  done
done

[ "$copied" -gt 0 ] || { echo "no master logos found at $SRC/logo-{light,dark}.{svg,png}" >&2; exit 1; }
