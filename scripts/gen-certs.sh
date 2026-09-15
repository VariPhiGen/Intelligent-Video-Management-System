#!/usr/bin/env bash
# gen-certs.sh — create a local CA + server cert for the Caddy TLS front door.
# Lives in repo-root scripts/; writes deploy/certs/ (mounted by the caddy service).
# Fully offline (openssl only) — suitable for air-gapped on-prem deployment.
# Run ON THE SERVER. SANs cover localhost, 127.0.0.1, the server's LAN IP and
# hostname so browsers on the LAN can validate the cert (after trusting ca.crt).
set -euo pipefail
cd "$(dirname "$0")/.."

OUT="deploy/certs"
DAYS="${CERT_DAYS:-825}"
# Pull the LAN IP from .env (SERVER_IP) if present, else this host's primary IP.
SERVER_IP="${SERVER_IP:-$(grep -E '^SERVER_IP=' .env 2>/dev/null | cut -d= -f2)}"
SERVER_IP="${SERVER_IP:-127.0.0.1}"
HOSTNAME_FQDN="$(hostname -f 2>/dev/null || hostname)"

mkdir -p "$OUT"

# Docker may have auto-created ./certs as root when the bind-mount source was
# missing at first `up`. Detect the resulting unwritable dir and explain clearly.
if [ ! -w "$OUT" ]; then
  echo "ERROR: '$OUT' is not writable by $(id -un) (likely root-owned, created by Docker)." >&2
  echo "Fix it, then re-run:  sudo rm -rf $OUT   (or: sudo chown -R \$USER:\$USER $OUT)" >&2
  exit 1
fi

if [ -f "$OUT/ca.crt" ] && [ "${FORCE:-}" != "1" ]; then
  echo "$OUT/ca.crt already exists — set FORCE=1 to regenerate. Skipping."
  exit 0
fi

echo "Generating CA + server cert (SANs: localhost, 127.0.0.1, ${SERVER_IP}, ${HOSTNAME_FQDN})"

# 1. Local CA
openssl genrsa -out "$OUT/ca.key" 4096
openssl req -x509 -new -nodes -key "$OUT/ca.key" -sha256 -days 3650 \
  -subj "/O=Variphi VMS/CN=Variphi VMS Local CA" -out "$OUT/ca.crt"

# 2. Server key + CSR
openssl genrsa -out "$OUT/server.key" 2048
openssl req -new -key "$OUT/server.key" \
  -subj "/O=Variphi VMS/CN=${SERVER_IP}" -out "$OUT/server.csr"

# 3. Sign with SANs
cat > "$OUT/san.ext" <<EXT
subjectAltName = DNS:localhost, DNS:${HOSTNAME_FQDN}, IP:127.0.0.1, IP:${SERVER_IP}
extendedKeyUsage = serverAuth
EXT
openssl x509 -req -in "$OUT/server.csr" -CA "$OUT/ca.crt" -CAkey "$OUT/ca.key" \
  -CAcreateserial -days "$DAYS" -sha256 -extfile "$OUT/san.ext" -out "$OUT/server.crt"

rm -f "$OUT/server.csr" "$OUT/san.ext" "$OUT/ca.srl"
chmod 600 "$OUT"/*.key

# Fail loudly if anything didn't land (so Caddy never sees a half-written dir).
for f in ca.crt server.crt server.key; do
  [ -s "$OUT/$f" ] || { echo "ERROR: $OUT/$f was not created." >&2; exit 1; }
done

cat <<EOF

Wrote:
  $OUT/ca.crt      <- import this into browsers/OS trust store (LAN clients)
  $OUT/server.crt  <- served by Caddy
  $OUT/server.key

Trust the CA so browsers don't warn:
  • Linux:  sudo cp $OUT/ca.crt /usr/local/share/ca-certificates/variphi-ca.crt && sudo update-ca-certificates
  • macOS:  sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain $OUT/ca.crt
  • Browsers: import ca.crt as a trusted Authority.

Then bring up the proxy:  docker compose up -d caddy
EOF
