#!/usr/bin/env bash
# gen-gpl-source.sh — the corresponding source for every GPL/LGPL component in
# an image, downloaded and archived so a release can publish it alongside the
# binaries.
#
# WHY THIS EXISTS. NOTICE §2 discharges the GPL source obligation by GPLv3
# §6(d) "equivalent access" rather than by the three-year written offer. That
# choice is lighter in every way but one: there is NO grace period. A release
# whose source archive is missing does not satisfy 6(d) on the day it is
# published, and cannot be cured later by answering a request. The source must
# be there when the image is. This script is how it gets there.
#
#   scripts/gen-gpl-source.sh            # all published images
#   scripts/gen-gpl-source.sh api nvr    # just these
#
# Output: dist/gpl-source/<image>/ with the .dsc, .orig.* and .debian.* files
# for each source package, plus MANIFEST.tsv (package, version, sha256).
#
# THE PROBLEM THIS SOLVES, AND HOW. Debian's archive is not a museum: the exact
# package version inside an image is usually gone from deb.debian.org within
# weeks, replaced by a point release. `apt-get source` against the live mirror
# therefore fetches source for a DIFFERENT version than the one shipped — which
# is not corresponding source, and does not satisfy anything.
#
# The fix is already in the image. Debian's own container builds record the
# snapshot they were built from, as a comment in the apt sources:
#
#     # http://snapshot.debian.org/archive/debian/20260824T000000Z
#     URIs: http://deb.debian.org/debian
#
# We read that timestamp back out and point deb-src at the snapshot, so the
# source fetched is the source that built the binaries — byte-for-byte the
# right version, however long ago the image was built. If a future base image
# stops recording it, this script fails rather than silently fetching the wrong
# source; see NO_SNAPSHOT below.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

ALL_IMAGES=(api nvr motion frames analytics smartsearch)
PREFIX="variphi-vms-"
OUT="dist/gpl-source"

say()  { printf '   %s\n' "$*" >&2; }
head_(){ printf '\n\033[1m── %s\033[0m\n' "$*" >&2; }
die()  { printf '\n!! %s\n' "$*" >&2; exit 1; }

if [ "$#" -gt 0 ]; then IMAGES=("$@"); else IMAGES=("${ALL_IMAGES[@]}"); fi

command -v docker >/dev/null 2>&1 || die "docker is required"

# The in-container half. Kept as one script so the whole fetch is a single
# `docker run`: apt state is throwaway, and nothing is written to the image.
read -r -d '' INNER <<'INNER_EOF' || true
set -eu

SRC=/etc/apt/sources.list.d/debian.sources
[ -f "$SRC" ] || { echo "NO_SOURCES"; exit 3; }

# The snapshot timestamps Debian's build recorded, in file order: the first is
# the main archive, the second (if present) is security.
SNAP_MAIN=$(grep -oE 'snapshot\.debian\.org/archive/debian/[0-9TZ]+' "$SRC" | head -1 | grep -oE '[0-9]{8}T[0-9]{6}Z' || true)
SNAP_SEC=$(grep -oE 'snapshot\.debian\.org/archive/debian-security/[0-9TZ]+' "$SRC" | head -1 | grep -oE '[0-9]{8}T[0-9]{6}Z' || true)
[ -n "$SNAP_MAIN" ] || { echo "NO_SNAPSHOT"; exit 4; }

. /etc/os-release
CODENAME="${VERSION_CODENAME:?}"

# Point deb-src at the snapshot. Check-Valid-Until must be off: a snapshot's
# Release file is as old as the snapshot, and apt rejects it otherwise.
{
  echo "Types: deb-src"
  echo "URIs: http://snapshot.debian.org/archive/debian/${SNAP_MAIN}"
  echo "Suites: ${CODENAME} ${CODENAME}-updates"
  echo "Components: main"
  echo "Check-Valid-Until: no"
  echo "Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp"
  if [ -n "$SNAP_SEC" ]; then
    echo
    echo "Types: deb-src"
    echo "URIs: http://snapshot.debian.org/archive/debian-security/${SNAP_SEC}"
    echo "Suites: ${CODENAME}-security"
    echo "Components: main"
    echo "Check-Valid-Until: no"
    echo "Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp"
  fi
  # AND the live archive. The snapshot above covers what the BASE image shipped;
  # it does not cover what our own Dockerfiles install, because `apt-get install`
  # at build time resolves against the live mirror and pulls whatever security
  # updates exist that day. Measured on variphi-vms-motion: the base snapshot
  # holds openssl 3.5.6-1~deb13u2 while the image runs 3.5.7-1~deb13u2, so a
  # snapshot-only fetch silently missed two of fifty-five packages. apt matches
  # on the exact version string, so listing both sources cannot fetch the wrong
  # source — it can only widen where the right one is found.
  echo
  echo "Types: deb-src"
  echo "URIs: http://deb.debian.org/debian"
  echo "Suites: ${CODENAME} ${CODENAME}-updates"
  echo "Components: main"
  echo "Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp"
  echo
  echo "Types: deb-src"
  echo "URIs: http://deb.debian.org/debian-security"
  echo "Suites: ${CODENAME}-security"
  echo "Components: main"
  echo "Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp"
} > /etc/apt/sources.list.d/gpl-src.sources

# snapshot.debian.org is rate-limited and occasionally slow; be patient rather
# than producing a half archive that looks complete.
apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=60 update >/dev/null 2>&1 \
  || { echo "APT_UPDATE_FAILED"; exit 5; }

mkdir -p /tmp/gplsrc && cd /tmp/gplsrc

# Which binary packages are GPL/LGPL, and what source package built each.
# Reading the copyright file is the same evidence gen-notice.sh attributes
# from, so the archive and the notices cannot disagree about what is GPL.
for d in /usr/share/doc/*/copyright; do
  [ -f "$d" ] || continue
  p=$(basename "$(dirname "$d")")
  grep -qiE 'GNU General Public|GNU Lesser General|^License:[[:space:]]*L?GPL' "$d" 2>/dev/null || continue
  sp=$(dpkg-query -W -f='${source:Package}' "$p" 2>/dev/null || true)
  sv=$(dpkg-query -W -f='${source:Version}' "$p" 2>/dev/null || true)
  [ -n "$sp" ] && [ -n "$sv" ] && echo "${sp}=${sv}"
done | sort -u > /tmp/wanted.txt

: > /tmp/failed.txt
while IFS= read -r spec; do
  [ -n "$spec" ] || continue
  apt-get -o Acquire::Retries=5 source --download-only "$spec" >/dev/null 2>&1 \
    || echo "$spec" >> /tmp/failed.txt
done < /tmp/wanted.txt

echo "WANTED=$(wc -l < /tmp/wanted.txt)"
echo "FAILED=$(wc -l < /tmp/failed.txt)"
[ -s /tmp/failed.txt ] && { echo "--- failed ---"; cat /tmp/failed.txt; }
exit 0
INNER_EOF

overall_fail=0
for short in "${IMAGES[@]}"; do
  img="${PREFIX}${short}"
  head_ "$img"
  docker image inspect "$img" >/dev/null 2>&1 || { say "SKIP  not built — run ./vms build $short"; continue; }

  dest="$OUT/$short"
  rm -rf "$dest"; mkdir -p "$dest"
  cid="gplsrc-$short-$$"

  set +e
  out=$(docker run --name "$cid" -u 0 --entrypoint sh "$img" -c "$INNER" 2>&1)
  rc=$?
  set -e

  case "$out" in
    *NO_SNAPSHOT*)
      docker rm -f "$cid" >/dev/null 2>&1 || true
      die "$img records no snapshot.debian.org timestamp — cannot prove the source
    matches the binaries. Do NOT publish this image until the base records one
    (Debian's official images do) or the source is fetched by other means." ;;
    *NO_SOURCES*|*APT_UPDATE_FAILED*)
      say "FAIL  could not reach the snapshot archive"; printf '%s\n' "$out" | sed 's/^/      /' >&2
      docker rm -f "$cid" >/dev/null 2>&1 || true; overall_fail=1; continue ;;
  esac
  [ "$rc" -eq 0 ] || { say "FAIL  rc=$rc"; printf '%s\n' "$out" | sed 's/^/      /' >&2; docker rm -f "$cid" >/dev/null 2>&1 || true; overall_fail=1; continue; }

  docker cp "$cid:/tmp/gplsrc/." "$dest/" >/dev/null 2>&1 || true
  docker rm -f "$cid" >/dev/null 2>&1 || true

  # MANIFEST is what a recipient checks the archive against.
  ( cd "$dest" && find . -maxdepth 1 -type f ! -name MANIFEST.tsv -print0 \
      | sort -z | xargs -0 -r shasum -a 256 \
      | sed 's|\*\?\./||' | awk '{print $2"\t"$1}' ) > "$dest/MANIFEST.tsv" 2>/dev/null || true

  files=$(find "$dest" -maxdepth 1 -type f ! -name MANIFEST.tsv | wc -l | tr -d ' ')
  dsc=$(find "$dest" -maxdepth 1 -name '*.dsc' | wc -l | tr -d ' ')
  printf '%s\n' "$out" | grep -E '^(WANTED|FAILED)=' | sed 's/^/      /' >&2
  if printf '%s\n' "$out" | grep -q '^FAILED=0$'; then
    say "ok    $dsc source package(s), $files file(s) → $dest"
  else
    say "PARTIAL  $dsc source package(s), $files file(s) → $dest"
    printf '%s\n' "$out" | sed -n '/--- failed ---/,$p' | sed 's/^/      /' >&2
    overall_fail=1
  fi
done

echo
if [ "$overall_fail" -eq 0 ]; then
  printf '\033[32mGPL source archived under %s\033[0m\n' "$OUT"
  echo "Publish this alongside the images, from the same place, at no charge (NOTICE §2)."
else
  printf '\033[31mIncomplete — do not publish a release with a partial source archive.\033[0m\n'
  exit 1
fi
