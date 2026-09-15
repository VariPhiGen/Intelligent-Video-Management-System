#!/usr/bin/env bash
# redis-to-valkey.sh — carry live keys from Redis 7.x across to Valkey 8.
#
# Why this exists: Valkey 8 cannot read an RDB written by Redis 7.4 — it exits
# with "Can't handle RDB format version 12". DUMP/RESTORE carries the same
# version stamp, so the only portable path is to read each key's value and TTL
# and write them again.
#
# Types handled: string and hash — verified to be the only types this VMS
# stores (audit session map and Keycloak high-water mark are strings, the
# discovery scan job is a hash, stream backoff and locks are strings). Anything
# else is reported rather than silently dropped, so a new type added later
# fails loudly here instead of vanishing during a migration.
#
# Values are base64-encoded in the dump file so that whitespace, newlines and
# tabs inside a value cannot corrupt the record separator.
#
# Losing the whole keyspace is survivable but not free: `audit:kc:high_water`
# has no TTL and is where the Keycloak audit sync resumes from. Dropping it
# re-reads or skips events. Session keys expire on their own; locks, backoff
# state and the scan job are rebuilt on demand.
#
# Usage:
#   scripts/redis-to-valkey.sh --dry-run   # report what would move, touch nothing
#   scripts/redis-to-valkey.sh --dump      # write the dump file only
#   scripts/redis-to-valkey.sh --cutover   # dump, back up, stop, clear the RDB
#   scripts/redis-to-valkey.sh --restore FILE   # after Valkey is up
set -euo pipefail

CONTAINER="${REDIS_CONTAINER:-vms_redis}"
VOLUME="${REDIS_VOLUME:-variphi-vms_redis_data}"
MODE="${1:---dry-run}"

command -v docker >/dev/null || { echo "docker not found" >&2; exit 1; }

cli() {
  docker exec "$CONTAINER" sh -c \
    'if command -v valkey-cli >/dev/null 2>&1; then valkey-cli "$@"; else redis-cli "$@"; fi' _ "$@"
}

require_container() {
  docker inspect "$CONTAINER" >/dev/null 2>&1 || {
    echo "container '$CONTAINER' not found — set REDIS_CONTAINER" >&2; exit 1; }
  [[ "$(docker inspect "$CONTAINER" --format '{{.State.Status}}')" == "running" ]] || {
    echo "container '$CONTAINER' is not running" >&2; exit 1; }
}

b64() { base64 -w0 2>/dev/null || base64; }

# ── dump ────────────────────────────────────────────────────────────────────
# Record formats, tab-separated, values base64:
#   S <key_b64> <ttl> <value_b64>
#   H <key_b64> <ttl> <field_b64>:<value_b64>,...
do_dump() {
  local out="$1" unsupported=0 n=0
  : > "$out"
  local keys; mapfile -t keys < <(cli --scan --count 500 | tr -d '\r' | sed '/^$/d')
  echo "  ${#keys[@]} keys in $CONTAINER" >&2
  local k t ttl
  for k in "${keys[@]}"; do
    t=$(cli type "$k" | tr -d '\r')
    ttl=$(cli ttl "$k" | tr -d '\r')
    case "$t" in
      string)
        printf 'S\t%s\t%s\t%s\n' \
          "$(printf '%s' "$k" | b64)" "$ttl" "$(cli --no-raw get "$k" | tr -d '\r' | sed 's/^"//;s/"$//' | b64)" >> "$out"
        n=$((n+1)) ;;
      hash)
        local pairs="" f v
        while IFS= read -r f && IFS= read -r v; do
          pairs+="$(printf '%s' "$f" | b64):$(printf '%s' "$v" | b64),"
        done < <(cli hgetall "$k" | tr -d '\r')
        printf 'H\t%s\t%s\t%s\n' "$(printf '%s' "$k" | b64)" "$ttl" "${pairs%,}" >> "$out"
        n=$((n+1)) ;;
      *)
        echo "  ! UNSUPPORTED type '$t' for key: $k" >&2
        unsupported=$((unsupported+1)) ;;
    esac
  done
  echo "  dumped $n keys → $out" >&2
  if (( unsupported )); then
    echo "  ! $unsupported key(s) of an unhandled type were NOT dumped — extend this script" >&2
    return 2
  fi
}

# ── restore ─────────────────────────────────────────────────────────────────
do_restore() {
  local in="$1" n=0
  [[ -r "$in" ]] || { echo "cannot read $in" >&2; exit 1; }
  local kind kb ttl rest key
  while IFS=$'\t' read -r kind kb ttl rest; do
    key=$(printf '%s' "$kb" | base64 -d)
    case "$kind" in
      S) cli set "$key" "$(printf '%s' "$rest" | base64 -d)" >/dev/null ;;
      H) local pair f v
         IFS=',' read -ra pairs <<< "$rest"
         for pair in "${pairs[@]}"; do
           f=$(printf '%s' "${pair%%:*}" | base64 -d)
           v=$(printf '%s' "${pair#*:}" | base64 -d)
           cli hset "$key" "$f" "$v" >/dev/null
         done ;;
      *) echo "  ! unknown record kind '$kind'" >&2; continue ;;
    esac
    [[ "$ttl" =~ ^[0-9]+$ ]] && cli expire "$key" "$ttl" >/dev/null
    n=$((n+1))
  done < "$in"
  echo "  restored $n keys; dbsize now $(cli dbsize | tr -d '\r')"
}

case "$MODE" in
  --dry-run)
    require_container
    echo "→ dry run (nothing is written)"
    do_dump "$(mktemp)" || true
    ;;
  --dump)
    require_container
    f="/tmp/vms-redis-keys.$(date +%s).tsv"
    echo "→ dumping"; do_dump "$f"; echo "$f"
    ;;
  --cutover)
    require_container
    f="/tmp/vms-redis-keys.$(date +%s).tsv"
    echo "→ dumping"; do_dump "$f"
    b="/tmp/vms-redis-data-$(date +%s).tar"
    echo "→ backing up the volume → $b"
    docker run --rm -v "$VOLUME":/src -v /tmp:/out alpine \
      tar cf "/out/$(basename "$b")" -C /src . >/dev/null
    echo "→ stopping $CONTAINER"; docker stop "$CONTAINER" >/dev/null
    echo "→ clearing the RDB Valkey cannot read"
    docker run --rm -v "$VOLUME":/data alpine \
      sh -c 'rm -f /data/dump.rdb /data/appendonly.aof; rm -rf /data/appendonlydir'
    echo
    echo "  Next:  docker compose up -d redis"
    echo "         scripts/redis-to-valkey.sh --restore $f"
    ;;
  --restore)
    shift; require_container
    echo "→ restoring into $CONTAINER"; do_restore "${1:?usage: --restore FILE}"
    ;;
  *) echo "usage: $0 [--dry-run|--dump|--cutover|--restore FILE]" >&2; exit 1 ;;
esac
