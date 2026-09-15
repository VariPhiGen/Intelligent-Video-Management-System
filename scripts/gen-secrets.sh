#!/usr/bin/env bash
# gen-secrets.sh — generate strong production secrets into .env (never committed).
#
# Two modes, because the safe thing to do differs completely between a machine
# that has never run and one that holds data:
#
#   (default)  Rotate INTERNAL_API_KEY and KEYCLOAK_ADMIN_PASSWORD only.
#              DISCOVERY_SECRET_KEY is deliberately NOT touched: it derives the
#              Fernet key for camera credentials at rest (backend/crypto.py), and
#              overwriting it makes every stored credential undecryptable —
#              silently, because decrypt() returns None rather than raising. On
#              an appliance with cameras, use scripts/rotate-secrets.sh, which
#              re-encrypts instead of orphaning.
#
#   --fresh    Also generate DISCOVERY_SECRET_KEY. Safe ONLY on an installation
#              with no encrypted camera credentials yet — there is nothing to
#              orphan. This is checked, not assumed (see is_fresh_install), and
#              the script refuses rather than guessing.
#
# --fresh exists because of a first-run trap: backend/main.py refuses to boot
# when dev_auth=False and any of the three secrets is still at a shipped
# default, and .env.example ships all three as placeholders. Without --fresh,
# `cp .env.example .env && ./vms up -d` — the documented quickstart — ends in a
# crash loop on a brand-new machine. ./vms now calls this automatically on first
# run so that never happens.
set -euo pipefail
cd "$(dirname "$0")/.."

FRESH=0
ENV_FILE=".env"
for a in "$@"; do
  case "$a" in
    --fresh) FRESH=1 ;;
    -h|--help) echo "usage: $0 [--fresh] [env-file]"; exit 0 ;;
    *) ENV_FILE="$a" ;;
  esac
done

if [ ! -f "$ENV_FILE" ]; then
  echo "No $ENV_FILE found — copy .env.example to .env first." >&2
  exit 1
fi

# URL/env-safe random string: base64, stripped of shell-special chars.
gen() { openssl rand -base64 "${1:-48}" | tr -d '\n/+=' | cut -c1-"${2:-48}"; }

set_kv() {
  local key="$1" val="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    sed -i.bak "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
  fi
}

# True only when nothing would be lost by changing DISCOVERY_SECRET_KEY.
# Deliberately conservative: anything we cannot verify counts as NOT fresh,
# because the failure mode is silent and unrecoverable.
is_fresh_install() {
  # THE VOLUME IS THE EVIDENCE, NOT THE CONTAINER. This used to answer "is the
  # postgres CONTAINER running?", which reports a brand-new machine and an
  # appliance whose stack is merely down as the same thing. They are not: the
  # second one has camera credentials on disk, and generating a new
  # DISCOVERY_SECRET_KEY orphans every one of them, silently and permanently —
  # exactly the loss this function exists to prevent. Nearly triggered on
  # 2026-09-09 by an appliance that had been torn down for a clean-room test:
  # 6 of its 7 cameras had stored credentials.
  #
  # Worse, `vms_postgres` is a HARDCODED container_name, so on a host running a
  # second compose project it could inspect the wrong database entirely.
  #
  # So: a data volume that exists counts as NOT fresh, whatever is running.
  if docker volume inspect "${COMPOSE_PROJECT_NAME:-variphi-vms}_postgres_data" \
       >/dev/null 2>&1; then
    # A volume with a real cluster in it means this box has state. Only treat
    # it as fresh if we can actually look inside and find no credentials.
    if ! docker inspect vms_postgres >/dev/null 2>&1 \
       || [ "$(docker inspect -f '{{.State.Status}}' vms_postgres 2>/dev/null)" != "running" ]; then
      echo "unreadable" > /tmp/.vms-cred-count
      return 1
    fi
  else
    # No data volume at all → nothing has ever been stored.
    docker inspect vms_postgres >/dev/null 2>&1 || return 0
    [ "$(docker inspect -f '{{.State.Status}}' vms_postgres 2>/dev/null)" = "running" ] || return 0
  fi

  local n
  n=$(docker exec vms_postgres psql -U "${POSTGRES_USER:-rtsp}" -d "${POSTGRES_DB:-rtsp_relay}" -tAc \
        "select count(*) from cameras where coalesce(enc_password,'')<>'';" 2>/dev/null || echo "ERR")

  # Table absent (fresh schema) reports an error, which is also fresh.
  case "$n" in
    ERR|"") return 0 ;;
    0)      return 0 ;;
    *)      echo "$n" > /tmp/.vms-cred-count; return 1 ;;
  esac
}

INTERNAL_API_KEY="$(gen 48 64)"
KEYCLOAK_ADMIN_PASSWORD="$(gen 32 32)"
set_kv "INTERNAL_API_KEY" "$INTERNAL_API_KEY"
set_kv "KEYCLOAK_ADMIN_PASSWORD" "$KEYCLOAK_ADMIN_PASSWORD"

# THE LOGINS SOMEONE WOULD ACTUALLY TYPE. KEYCLOAK_ADMIN_PASSWORD above is the
# Keycloak master console; these two are the application accounts seeded by
# deploy/keycloak/realm-vms.json, which shipped them as the literals
# `admin`/`admin` and `operator`/`operator`. Rotating three secrets and leaving
# those was the gap. deploy/keycloak/sync-client.py applies these on the first
# `up` and marks each user so a later run never overwrites a password the
# operator has since chosen.
#
# `admin` is always seeded. `operator` is seeded only where the realm file still
# carries it — the public tree's realm seeds admin alone. A password generated
# for an account that does not exist is a login printed for nobody, so ask the
# realm rather than assume.
VMS_ADMIN_PASSWORD="$(gen 24 24)"
set_kv "VMS_ADMIN_PASSWORD" "$VMS_ADMIN_PASSWORD"
SIGNIN_LINES="  VMS_ADMIN_PASSWORD       = ${VMS_ADMIN_PASSWORD}   (sign in as 'admin')"
if grep -qE '"username"[[:space:]]*:[[:space:]]*"operator"' deploy/keycloak/realm-vms.json 2>/dev/null; then
  VMS_OPERATOR_PASSWORD="$(gen 24 24)"
  set_kv "VMS_OPERATOR_PASSWORD" "$VMS_OPERATOR_PASSWORD"
  SIGNIN_LINES="${SIGNIN_LINES}
  VMS_OPERATOR_PASSWORD    = ${VMS_OPERATOR_PASSWORD}   (sign in as 'operator')"
fi

DSK_NOTE="  DISCOVERY_SECRET_KEY     left untouched (see --fresh, and rotate-secrets.sh)"
SDB_NOTE="  SEARCHDB_PASSWORD        left untouched (rotating it needs ALTER ROLE — see below)"
if [ "$FRESH" -eq 1 ]; then
  if is_fresh_install; then
    set_kv "DISCOVERY_SECRET_KEY" "$(gen 48 48)"
    DSK_NOTE="  DISCOVERY_SECRET_KEY     generated (no stored camera credentials to orphan)"

    # Smart Search's pgvector database. Safe to generate ONLY on a fresh
    # install: postgres bakes POSTGRES_PASSWORD into the role at initdb, so on
    # a box whose searchdb volume already exists a new value here would simply
    # fail to authenticate. is_fresh_install above is the same gate the
    # discovery key uses.
    #
    # BOTH KEYS MOVE TOGETHER OR NEITHER DOES. SEARCHDB_URL carries its own
    # copy of the password (.env.example ships the literal twice), so setting
    # SEARCHDB_PASSWORD alone leaves the URL pointing at the old one and Smart
    # Search cannot reach its own database.
    SEARCHDB_PASSWORD="$(gen 32 32)"
    SEARCHDB_USER_V="$(grep -E '^SEARCHDB_USER=' "$ENV_FILE" | cut -d= -f2-)"
    SEARCHDB_NAME_V="$(grep -E '^SEARCHDB_NAME=' "$ENV_FILE" | cut -d= -f2-)"
    set_kv "SEARCHDB_PASSWORD" "$SEARCHDB_PASSWORD"
    set_kv "SEARCHDB_URL" \
      "postgresql://${SEARCHDB_USER_V:-search}:${SEARCHDB_PASSWORD}@127.0.0.1:5434/${SEARCHDB_NAME_V:-smartsearch}"
    SDB_NOTE="  SEARCHDB_PASSWORD        generated (SEARCHDB_URL updated to match)"
  else
    rm -f "${ENV_FILE}.bak"
    cat >&2 <<EOF

Refusing --fresh: this installation already has $(cat /tmp/.vms-cred-count 2>/dev/null || echo some) camera(s)
with stored credentials. Generating a new DISCOVERY_SECRET_KEY would make them
permanently undecryptable, and the failure would be silent — the cameras would
simply appear to have forgotten their passwords.

INTERNAL_API_KEY and KEYCLOAK_ADMIN_PASSWORD were still rotated.

To rotate the discovery key safely, re-encrypting as it goes:
    scripts/rotate-secrets.sh --rotate
EOF
    rm -f /tmp/.vms-cred-count
    exit 1
  fi
fi
rm -f "${ENV_FILE}.bak" /tmp/.vms-cred-count

cat <<EOF

Secrets written to $ENV_FILE:
  INTERNAL_API_KEY         (trusted machine-to-machine callers) — rotated
  KEYCLOAK_ADMIN_PASSWORD  = ${KEYCLOAK_ADMIN_PASSWORD}   (Keycloak console)
${SIGNIN_LINES}
${DSK_NOTE}
${SDB_NOTE}

WRITE THE SIGN-IN PASSWORDS DOWN NOW — they are not shown again, and they
replace the username-as-password literals the realm file ships.
EOF

if [ "$FRESH" -eq 1 ]; then
  # FIRST RUN. Nothing is running yet, so there is nothing to restart and no
  # Keycloak realm to reconcile — `./vms up -d` is about to start everything
  # with these values. Printing the rotation recipe here (as this script used
  # to) hands a newcomer instructions for a situation they are not in.
  cat <<'EOF'
Next step:
  ./vms up -d
EOF
else
  cat <<'EOF'
Next steps (ROTATION on a running appliance):
  1. Restart the service that reads INTERNAL_API_KEY:
        ./vms up -d api
  2. KEYCLOAK_ADMIN_PASSWORD only takes effect on a FRESH Keycloak (bootstrap).
     If the keycloak DB already exists, either change the master admin password
     in the Keycloak console, or reset it:
        ./vms stop keycloak
        docker exec vms_postgres psql -U ${POSTGRES_USER:-rtsp} -d ${POSTGRES_DB:-rtsp_relay} \
          -c "DROP DATABASE IF EXISTS keycloak WITH (FORCE);"
        ./vms run --rm keycloak_db_init && ./vms up -d keycloak
  3. VMS_ADMIN_PASSWORD (and VMS_OPERATOR_PASSWORD, where the realm seeds
     operator) are applied once per user, on the first `up` after the realm is
     imported. On an appliance whose realm already exists, change them in the
     Keycloak console instead —
     sync-client.py will not overwrite a password an operator has chosen.
EOF
fi

cat <<'EOF'

Note: POSTGRES_PASSWORD rotation is intentionally NOT automated (it needs an
ALTER ROLE plus updating DATABASE_URL / DISCOVERY_DATABASE_URL / KC_DB_PASSWORD).
Postgres is bound to 127.0.0.1 only, so rotate it during a maintenance window.
SEARCHDB_PASSWORD is now generated on --fresh (with SEARCHDB_URL kept in step),
but rotating it on an EXISTING appliance has the same ALTER ROLE requirement:
postgres baked the old value into the role at initdb.
EOF
