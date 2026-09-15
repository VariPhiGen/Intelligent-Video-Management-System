#!/usr/bin/env bash
# rotate-secrets.sh — replace the shipped-default secrets, WITHOUT orphaning the
# camera credentials already encrypted under the old key.
#
# Why this exists and gen-secrets.sh is not enough:
#
#   The API now refuses to boot when dev_auth=False and any of INTERNAL_API_KEY,
#   DISCOVERY_SECRET_KEY or KEYCLOAK_ADMIN_PASSWORD is still at a shipped or weak
#   default (backend/main.py, via Settings.insecure_default_secrets). So these
#   must be rotated before the next rebuild — but two of the three cannot simply
#   be overwritten:
#
#   • DISCOVERY_SECRET_KEY derives the Fernet key for camera credentials at rest
#     (crypto.py: Fernet(b64(sha256(key)))). Overwrite it and every stored
#     enc_username / enc_password becomes undecryptable — and decrypt() returns
#     None rather than raising, so the failure is SILENT and presents as "the
#     camera forgot its password". gen-secrets.sh deliberately won't touch it.
#     This script decrypts with the old key and re-encrypts with the new one in a
#     single transaction: nothing is lost, nothing is re-entered.
#
#   • KEYCLOAK_ADMIN_PASSWORD is used at RUNTIME — keycloak_admin.py fetches a
#     master-realm token with it for every user-management call. Changing only
#     .env satisfies the boot guard but breaks the Administration page. This
#     script changes it in Keycloak first, then in .env.
#
#   • INTERNAL_API_KEY is the easy one: only the api validates it and no in-repo
#     caller sends it. An EXTERNAL integration might (audit ingest, a discovery
#     microservice) — hand those the new value.
#
# Usage:
#   scripts/rotate-secrets.sh --check     # report only, writes nothing
#   scripts/rotate-secrets.sh --rotate    # do it
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

ENV_FILE="${ENV_FILE:-.env}"
MODE="${1:---check}"
TS="$(date +%s)"
WORK="$(mktemp -d)"; chmod 700 "$WORK"
CRYPTO_IN_CONTAINER="/tmp/.vms-rekey-$TS.py"
cleanup() { rm -rf "$WORK"; docker exec vms_api rm -f "$CRYPTO_IN_CONTAINER" 2>/dev/null || true; }
trap cleanup EXIT

b()   { printf '\n\033[1m── %s\033[0m\n' "$*"; }
say() { printf '   %s\n' "$*"; }
die() { printf '\n!! %s\n' "$*" >&2; exit 1; }

gen()     { openssl rand -base64 "${1:-48}" | tr -d '\n/+=' | cut -c1-"${2:-48}"; }
envget()  { sed -n "s/^$1=//p" "$ENV_FILE" | head -1; }
running() { [ "$(docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null)" = "running" ]; }

MARKERS='change-me|change-for-production|changeme|insecure'
is_weak() {
  local v; v="$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')"
  [ -z "$v" ] && return 0
  printf '%s' "$v" | grep -qE "$MARKERS" && return 0
  case "$1" in
    KEYCLOAK_ADMIN_PASSWORD) case "$v" in admin|password|keycloak) return 0 ;; esac ;;
  esac
  return 1
}

[ -f "$ENV_FILE" ] || die "no $ENV_FILE here"

OLD_DSK="$(envget DISCOVERY_SECRET_KEY)"
OLD_KCP="$(envget KEYCLOAK_ADMIN_PASSWORD)"
KC_USER="$(envget KEYCLOAK_ADMIN)"; KC_USER="${KC_USER:-admin}"
DEV_AUTH="$(envget DEV_AUTH)"

# Keycloak's port is NOT 8080 here. The stack runs network_mode: host and
# Keycloak binds KEYCLOAK_PORT (8085), so the container's own localhost:8080 is
# closed — kcadm against it fails with "Connection refused", which reads exactly
# like a wrong password and is not. Derive it from .env, preferring an explicit
# KEYCLOAK_ADMIN_URL when the site sets one.
KC_SERVER="$(envget KEYCLOAK_ADMIN_URL)"
if [ -z "$KC_SERVER" ]; then
  KC_PORT="$(envget KEYCLOAK_PORT)"; KC_SERVER="http://localhost:${KC_PORT:-8085}"
fi

dump_rows() {
  docker exec vms_postgres psql -U rtsp -d rtsp_relay -tAc \
    "select id||'|'||coalesce(enc_username,'')||'|'||coalesce(enc_password,'')
       from cameras
      where coalesce(enc_username,'')<>'' or coalesce(enc_password,'')<>'';"
}

# The helper is COPIED INTO the container rather than fed to `python -` on stdin:
# `docker exec -i` hands the heredoc to the interpreter as its program text,
# which leaves sys.stdin exhausted and every row silently unread — it reports a
# clean "0 tokens" and looks like success. Keys and rows arrive on stdin instead,
# so they never appear in `ps` or the shell history.
crypto_install() {
  cat > "$WORK/rekey.py" <<'PYEOF'
import base64, hashlib, sys
from cryptography.fernet import Fernet, InvalidToken

mode = sys.stdin.readline().strip()
old  = sys.stdin.readline().rstrip("\n")
new  = sys.stdin.readline().rstrip("\n")
rows = [l.rstrip("\n") for l in sys.stdin if l.strip()]

def fern(k):
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(k.encode()).digest()))

fo = fern(old)
fn = fern(new) if new else None

ok = bad = 0
out = []
for line in rows:
    cid, u, p = line.split("|", 2)
    vals = {}
    for label, tok in (("enc_username", u), ("enc_password", p)):
        if not tok:
            continue
        try:
            clear = fo.decrypt(tok.encode())
            ok += 1
        except InvalidToken:
            bad += 1
            sys.stderr.write("UNDECRYPTABLE\t%s\t%s\n" % (cid, label))
            continue
        if fn is not None:
            vals[label] = fn.encrypt(clear).decode()
    if mode == "reencrypt" and vals:
        sets = ", ".join("%s = '%s'" % (k, v) for k, v in vals.items())
        out.append("UPDATE cameras SET %s WHERE id = '%s';" % (sets, cid))

sys.stderr.write("TOKENS\t%d\t%d\n" % (ok, bad))
if bad:
    sys.exit(3)
sys.stdout.write("\n".join(out))
PYEOF
  docker cp "$WORK/rekey.py" "vms_api:$CRYPTO_IN_CONTAINER" >/dev/null
}

# stdin: mode / old / new / rows…   stdout: UPDATEs   stderr: TOKENS, UNDECRYPTABLE
crypto_pass() { docker exec -i vms_api python "$CRYPTO_IN_CONTAINER"; }

# ── check ───────────────────────────────────────────────────────────────────
do_check() {
  b "Boot guard (backend/main.py)"
  if [ "${DEV_AUTH:-}" = "true" ]; then
    say "DEV_AUTH = true  (guard inactive — dev posture)"
  else
    say "DEV_AUTH = ${DEV_AUTH:-unset}  (guard ACTIVE — production posture)"
  fi
  local off=0 n
  for n in INTERNAL_API_KEY DISCOVERY_SECRET_KEY KEYCLOAK_ADMIN_PASSWORD; do
    if is_weak "$n" "$(envget "$n")"; then say "INSECURE  $n"; off=$((off+1))
    else say "ok        $n"; fi
  done
  if [ "$off" -gt 0 ]; then
    say "→ the api will refuse to start on the next rebuild ($off offender(s))"
  else
    say "→ would boot"
  fi

  b "Camera credentials at rest"
  running vms_postgres || { say "vms_postgres not running — cannot check"; return 0; }
  running vms_api      || { say "vms_api not running — cannot check (its cryptography is used)"; return 0; }
  crypto_install
  dump_rows > "$WORK/rows"
  local rows; rows=$(wc -l < "$WORK/rows")
  say "$rows camera row(s) hold an encrypted credential"
  if [ "$rows" -gt 0 ]; then
    { printf 'verify\n%s\n\n' "$OLD_DSK"; cat "$WORK/rows"; } \
      | crypto_pass >/dev/null 2>"$WORK/err" || true
    sed -n 's/^TOKENS\t\([0-9]*\)\t\([0-9]*\)$/   \1 token(s) decrypt with the current key, \2 do not/p' "$WORK/err"
    grep '^UNDECRYPTABLE' "$WORK/err" | sed 's/^/   /' || true
  fi
  echo
}

# ── rotate ──────────────────────────────────────────────────────────────────
do_rotate() {
  running vms_postgres || die "vms_postgres is not running"
  running vms_api      || die "vms_api is not running (its cryptography does the re-encryption)"
  running vms_keycloak || die "vms_keycloak is not running (the admin password is changed there first)"
  crypto_install

  b "1. Pre-flight — every credential must decrypt with the CURRENT key"
  dump_rows > "$WORK/rows"
  local nrows; nrows=$(wc -l < "$WORK/rows")
  say "$nrows camera row(s) to re-key"
  if [ "$nrows" -gt 0 ]; then
    if ! { printf 'verify\n%s\n\n' "$OLD_DSK"; cat "$WORK/rows"; } \
         | crypto_pass >/dev/null 2>"$WORK/err"; then
      grep '^UNDECRYPTABLE' "$WORK/err" | sed 's/^/   /' >&2 || true
      die "some credentials do not decrypt with the current key — refusing to rotate.
    Rotating now would destroy them permanently. Re-enter those cameras'
    credentials in the UI first, then run this again."
    fi
    sed -n 's/^TOKENS\t\([0-9]*\)\t.*/   \1 token(s) verified/p' "$WORK/err"
  fi

  local REVERT
  b "2. Backups"
  cp "$ENV_FILE" "$ENV_FILE.bak-$TS"; chmod 600 "$ENV_FILE.bak-$TS"
  say "$ENV_FILE.bak-$TS"
  docker exec vms_postgres pg_dump -U rtsp -d rtsp_relay -t cameras --data-only \
    > "/tmp/vms-cameras-$TS.sql"
  say "/tmp/vms-cameras-$TS.sql ($(wc -l < "/tmp/vms-cameras-$TS.sql") lines) — full table"
  # Also emit a SURGICAL restore: UPDATEs touching only the two encrypted
  # columns. Restoring the --data-only dump means emptying `cameras` first, and
  # TRUNCATE ... CASCADE would take analytics_events (FK ON DELETE CASCADE) with
  # it. Recovery should never cost more than it repairs.
  REVERT="/tmp/vms-cameras-revert-$TS.sql"
  {
    echo "BEGIN;"
    while IFS='|' read -r cid u p; do
      [ -n "$cid" ] || continue
      echo "UPDATE cameras SET enc_username='$u', enc_password='$p' WHERE id='$cid';"
    done < "$WORK/rows"
    echo "COMMIT;"
  } > "$REVERT"
  chmod 600 "$REVERT"
  say "$REVERT — surgical revert ($nrows row(s), credential columns only)"

  b "3. Generate and stash"
  local NEW_IAK NEW_DSK NEW_KCP PENDING
  NEW_IAK="$(gen 48 64)"; NEW_DSK="$(gen 48 48)"; NEW_KCP="$(gen 32 32)"
  # Write them to disk BEFORE anything irreversible uses them. The first version
  # of this script generated the key, re-encrypted the database with it, then
  # died on the next step — taking the only copy of the key with the process and
  # leaving every credential encrypted under something that no longer existed.
  # Recoverable only because a backup had been taken. Nothing may consume a
  # generated secret until that secret survives a crash.
  PENDING="$ENV_FILE.pending-$TS"
  ( umask 077; printf 'INTERNAL_API_KEY=%s\nDISCOVERY_SECRET_KEY=%s\nKEYCLOAK_ADMIN_PASSWORD=%s\n' \
      "$NEW_IAK" "$NEW_DSK" "$NEW_KCP" > "$PENDING" )
  say "stashed in $PENDING (0600) — survives any failure below"

  b "4. Keycloak master-admin password"
  # Before .env, so a failure here leaves .env — and therefore the running api —
  # consistent with the password Keycloak still holds.
  docker exec -i vms_keycloak /opt/keycloak/bin/kcadm.sh config credentials \
      --server "$KC_SERVER" --realm master \
      --user "$KC_USER" --password "$OLD_KCP" >/dev/null 2>&1 \
    || die "could not authenticate to Keycloak at $KC_SERVER as '$KC_USER'.
    Nothing has been changed — the database re-key has not run yet.
    Check the server/port and the password, then re-run. Delete $PENDING." 
  docker exec -i vms_keycloak /opt/keycloak/bin/kcadm.sh set-password \
      -r master --username "$KC_USER" --new-password "$NEW_KCP" >/dev/null 2>&1 \
    || die "could not set the new password in Keycloak. Nothing else has changed —
    the database re-key has not run yet. Delete $PENDING and re-run." 
  say "changed in Keycloak for user '$KC_USER' (master realm, $KC_SERVER)"

  b "5. Re-encrypt camera credentials (single transaction)"
  if [ "$nrows" -gt 0 ]; then
    { printf 'reencrypt\n%s\n%s\n' "$OLD_DSK" "$NEW_DSK"; cat "$WORK/rows"; } \
      | crypto_pass > "$WORK/updates.sql" 2>"$WORK/err2" \
      || die "re-encryption failed — nothing was written"
    { echo "BEGIN;"; cat "$WORK/updates.sql"; echo; echo "COMMIT;"; } > "$WORK/tx.sql"
    docker exec -i vms_postgres psql -U rtsp -d rtsp_relay -v ON_ERROR_STOP=1 -q < "$WORK/tx.sql" \
      || die "transaction failed and rolled back — nothing lost.\n    Keycloak already has the new password: it is in $PENDING."
    say "$(grep -c '^UPDATE' "$WORK/updates.sql") row update(s) committed"

    dump_rows > "$WORK/rows2"
    { printf 'verify\n%s\n\n' "$NEW_DSK"; cat "$WORK/rows2"; } \
      | crypto_pass >/dev/null 2>"$WORK/err3" \
      || die "re-encrypted rows do not decrypt with the new key. RESTORE NOW:
    docker exec -i vms_postgres psql -U rtsp -d rtsp_relay < $REVERT"
    sed -n 's/^TOKENS\t\([0-9]*\)\t.*/   \1 token(s) verified under the NEW key/p' "$WORK/err3"
  else
    say "no rows to re-key"
  fi

  b "6. Write $ENV_FILE"
  python3 - "$ENV_FILE" "$NEW_IAK" "$NEW_DSK" "$NEW_KCP" <<'PYEOF'
import sys, pathlib
path, iak, dsk, kcp = sys.argv[1:5]
vals = {"INTERNAL_API_KEY": iak, "DISCOVERY_SECRET_KEY": dsk,
        "KEYCLOAK_ADMIN_PASSWORD": kcp}
out, seen = [], set()
for line in pathlib.Path(path).read_text().splitlines(keepends=True):
    k = None
    if "=" in line and not line.lstrip().startswith("#"):
        k = line.split("=", 1)[0].strip()
    if k in vals:
        out.append("%s=%s\n" % (k, vals[k])); seen.add(k)
    else:
        out.append(line)
for k, v in vals.items():
    if k not in seen:
        out.append("%s=%s\n" % (k, v))
pathlib.Path(path).write_text("".join(out))
print("   3 value(s) written")
PYEOF
  chmod 600 "$ENV_FILE"
  rm -f "$PENDING"
  say "stash removed — $ENV_FILE is now authoritative"

  b "Done — restart so every service picks up the new values"
  cat <<EOF
   ./vms build api && ./vms up -d

   The window between step 4 and that restart is the only exposure: the running
   api still holds the OLD key, so a discovery or probe call would fail to
   decrypt a camera credential. Recording, playback and live view never touch
   these — they use the relay URL, not the camera password.

   Rollback (surgical — touches only the credential columns):
     cp $ENV_FILE.bak-$TS $ENV_FILE
     docker exec -i vms_postgres psql -U rtsp -d rtsp_relay < $REVERT

   If an EXTERNAL integration sends X-Internal-Key (audit ingest, a discovery
   microservice), hand it the new INTERNAL_API_KEY.
EOF
}

case "$MODE" in
  --check)  do_check ;;
  --rotate) do_check; do_rotate ;;
  *) echo "usage: $0 [--check|--rotate]" >&2; exit 1 ;;
esac
