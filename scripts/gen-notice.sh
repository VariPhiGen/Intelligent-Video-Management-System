#!/usr/bin/env bash
# gen-notice.sh — regenerate THIRD-PARTY-NOTICES.md from the IMAGES we actually
# ship, not from the manifests that describe our intent.
#
# Why generated and not written: the service images carry ~1,500 components
# between them (roughly 300 Debian packages each, a few hundred Python
# distributions, 126 npm packages in the SPA lockfile). A hand-maintained
# attribution file is wrong the day it is written and wrong differently after
# every base-image refresh. Attribution that is not regenerated is not
# attribution, it is a snapshot of a past release.
#
# EVERY SHIPPED IMAGE MUST BE LISTED IN `IMAGES`, and that list is the one thing
# here a human has to keep right. It was wrong between 2026-09-09 and the commit
# that added this note: `9f5a5ec` split detection into services/analytics and the
# broker into services/frames, and this list still named three images. The result
# was that ultralytics — **AGPL-3.0**, the strongest obligation in the tree —
# together with torch, openvino, onnx, onnxslim, fast-plate-ocr and
# open-image-models were absent from the legally operative file for thirteen
# days, while services/analytics/analytics/detector.py documented the AGPL risk
# perfectly. The code knew; the notices did not.
#
# A MISSING IMAGE IS NOW FATAL, not a warning. This previously said "SKIP (its
# components will be MISSING)" on stderr and then wrote the file anyway — so a
# partial inventory was indistinguishable from a complete one once committed,
# which is precisely how the gap above survived being regenerated.
#
# Why from the image: `apt`/`pip`/`npm` manifests say what we asked for; the
# image says what a user receives. Debian's DEP-5 /usr/share/doc/<pkg>/copyright
# is the authoritative per-package record and is already machine-readable, so
# this reads that rather than guessing from package names.
#
# NEVER GUESSES. A component whose licence cannot be determined is emitted as
# UNDETERMINED and counted in the summary, so it shows up as work to do instead
# of silently becoming "probably MIT".
#
# Usage:
#   scripts/gen-notice.sh            # regenerate THIRD-PARTY-NOTICES.md
#   scripts/gen-notice.sh --check    # CI: fail if the file is stale or unsafe
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

OUT="THIRD-PARTY-NOTICES.md"
MODE="${1:-}"
# Every image this repository publishes. Six services ship; all six are here.
# `frames`, `analytics` and `smartsearch` are tier 1 — none of them is listed in
# .publicignore — so their dependencies are distributed and must be attributed.
IMAGES=(
  variphi-vms-api
  variphi-vms-nvr
  variphi-vms-motion
  variphi-vms-frames
  variphi-vms-analytics       # ultralytics: AGPL-3.0. See detector.py's header.
  variphi-vms-smartsearch     # open_clip_torch -> torch
)
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

say()  { printf '   %s\n' "$*" >&2; }
head_() { printf '\n\033[1m── %s\033[0m\n' "$*" >&2; }
die()  { printf '\n!! %s\n' "$*" >&2; exit 1; }

have_image() { docker image inspect "$1" >/dev/null 2>&1; }

# ── The one check that is not about attribution ─────────────────────────────
# `--enable-nonfree` makes an ffmpeg binary UNREDISTRIBUTABLE outright — not a
# licensing obligation, a hard bar on shipping at all. Debian does not build
# with it (its own copyright file says so explicitly), but a future switch to a
# hand-rolled or third-party ffmpeg image could reintroduce it silently. This
# turns that into a build failure instead of a legal discovery.
assert_no_nonfree() {
  local img bad=0
  for img in "${IMAGES[@]}"; do
    have_image "$img" || continue
    if docker run --rm --entrypoint sh "$img" -c \
         'command -v ffmpeg >/dev/null 2>&1 && ffmpeg -hide_banner -version 2>/dev/null || true' \
       | grep -q -- '--enable-nonfree'; then
      say "FAIL  $img ships an ffmpeg built --enable-nonfree — NOT redistributable"
      bad=1
    fi
  done
  [ "$bad" -eq 0 ] || die "refusing to generate notices for an unshippable image"
  say "ok    no --enable-nonfree ffmpeg in any image"
}

# ── Debian packages, via DEP-5 copyright files ──────────────────────────────
debian_components() {   # $1 = image ; emits: name \t version \t licences
  docker run --rm --entrypoint sh "$1" -c '
    for d in /usr/share/doc/*/copyright; do
      [ -f "$d" ] || continue
      p=$(basename "$(dirname "$d")")
      v=$(dpkg-query -W -f="\${Version}" "$p" 2>/dev/null || true)
      l=$(grep -hE "^License:" "$d" 2>/dev/null \
          | sed "s/^License:[[:space:]]*//" | sed "/^$/d" | sort -u \
          | paste -sd "," - || true)
      # Not every Debian copyright file is DEP-5 — the older free-form ones have
      # no License: field at all. Read the licence out of the body instead and
      # TAG IT as text-derived, so a declared field is never confused with a
      # phrase match. Still not a guess: these are verbatim licence names.
      if [ -z "$l" ]; then
        t=""
        grep -qiE "GNU Lesser General Public|LGPL" "$d" 2>/dev/null && t="LGPL"
        if [ -z "$t" ]; then grep -qiE "GNU General Public|GPL" "$d" 2>/dev/null && t="GPL"; fi
        if [ -z "$t" ]; then grep -qiE "Apache License" "$d" 2>/dev/null && t="Apache"; fi
        if [ -z "$t" ]; then grep -qiE "MIT License|Permission is hereby granted, free of charge" "$d" 2>/dev/null && t="MIT"; fi
        # X11/MIT permission notice — used verbatim by X.Org and fontconfig.
        # It never contains the string "MIT", which is why those packages came
        # back undetermined against the clause above.
        if [ -z "$t" ]; then grep -qiE "Permission to use, copy, modify, distribute, and sell this software" "$d" 2>/dev/null && t="MIT/X11"; fi
        if [ -z "$t" ]; then grep -qiE "Redistribution and use in source and binary" "$d" 2>/dev/null && t="BSD"; fi
        if [ -z "$t" ]; then grep -qiE "Mozilla Public License" "$d" 2>/dev/null && t="MPL"; fi
        if [ -z "$t" ]; then grep -qiE "public domain" "$d" 2>/dev/null && t="Public-domain"; fi
        [ -n "$t" ] && l="$t (from copyright text)"
      fi
      [ -n "$l" ] || l="UNDETERMINED"
      printf "%s\t%s\t%s\n" "$p" "${v:-unknown}" "$l"
    done' 2>/dev/null
}

# ── Python distributions ────────────────────────────────────────────────────
# `python -` takes the program on stdin, so the program itself must need no
# stdin. It does not — everything comes from importlib.metadata.
cat > "$WORK/pymeta.py" <<'PYEOF'
import sys
try:
    from importlib.metadata import distributions
except Exception:
    sys.exit(0)


def from_license_file(dist):
    """Licence read out of the LICENSE file the wheel actually ships.

    STILL NOT A GUESS, AND IT SAYS SO. This is the same fallback
    debian_components() already applies to non-DEP-5 copyright files: match
    verbatim licence wording and TAG the result as text-derived, so a declared
    licence is never confused with one inferred from a body of text. The
    alternative for a distribution that declares nothing at all — setuptools
    79.0.1 publishes no License, no License-Expression and no classifiers — is
    UNDETERMINED, which is honest but leaves a shipped file unaccounted for when
    the answer is sitting in the wheel.
    """
    names = dist.metadata.get_all("License-File") or []
    if not names:
        return ""
    for rel in names:
        # Only the distribution's OWN licence. torch declares ninety-odd
        # third_party/* files; those belong to components vendored inside it and
        # attributing torch from one of them would name the wrong licence.
        if "/" in rel.replace("\\", "/"):
            continue
        try:
            for f in (dist.files or []):
                if str(f).endswith(rel):
                    text = f.read_text()
                    break
            else:
                continue
        except Exception:
            continue
        if not text:
            continue
        for pattern, label in (
            ("GNU AFFERO GENERAL PUBLIC LICENSE", "AGPL (from licence text)"),
            ("GNU LESSER GENERAL PUBLIC LICENSE", "LGPL (from licence text)"),
            ("GNU GENERAL PUBLIC LICENSE", "GPL (from licence text)"),
            ("APACHE LICENSE", "Apache (from licence text)"),
            ("PERMISSION IS HEREBY GRANTED, FREE OF CHARGE", "MIT (from licence text)"),
            ("REDISTRIBUTION AND USE IN SOURCE AND BINARY", "BSD (from licence text)"),
            ("MOZILLA PUBLIC LICENSE", "MPL (from licence text)"),
            ("BOOST SOFTWARE LICENSE", "BSL (from licence text)"),
        ):
            if pattern in text.upper():
                return label
    return ""


seen = set()
for d in distributions():
    try:
        m = d.metadata
        name = m["Name"]
        if not name or name in seen:
            continue
        seen.add(name)
        # PEP 639 replaced the free-text License field with License-Expression
        # (an SPDX string). Newer wheels set only that, which is why they came
        # back UNDETERMINED when we looked at License alone.
        #
        # THE LENGTH HEURISTIC APPLIES TO THE FREE-TEXT FIELD ONLY. It exists
        # because the old `License:` field is unstructured and some wheels put
        # an entire licence body in it; a 400-character "licence name" is not a
        # name. But License-Expression is SPDX BY DEFINITION, and a long one is
        # long because the package really is multi-licensed. Applying the cap to
        # it discarded a perfectly good declaration: torch 2.14 states
        #   "Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause
        #    AND BSD-3-Clause AND BSL-1.0 AND MIT"
        # — 96 characters, no classifiers to fall back to, so it was reported as
        # UNDETERMINED while declaring its licence perfectly well.
        lic = (m.get("License-Expression") or "").strip()
        if not lic:
            free = (m.get("License") or "").strip().splitlines()
            lic = free[0].strip() if free else ""
            if len(lic) > 60:
                lic = ""
        if not lic:
            cls = [c.split("::")[-1].strip()
                   for c in (m.get_all("Classifier") or [])
                   if c.startswith("License ::")]
            lic = ", ".join(cls) if cls else ""
        if not lic:
            lic = from_license_file(d)
        print("%s\t%s\t%s" % (name, d.version or "unknown", lic or "UNDETERMINED"))
    except Exception:
        continue
PYEOF

python_components() {   # $1 = image
  docker run --rm -i --entrypoint python "$1" - < "$WORK/pymeta.py" 2>/dev/null || true
}

# ── npm packages compiled into the shipped SPA bundle ───────────────────────
npm_components() {
  [ -f frontend-react/package-lock.json ] || return 0
  python3 - <<'PYEOF'
import json
d = json.load(open("frontend-react/package-lock.json"))
for path, meta in (d.get("packages") or {}).items():
    if not path:
        continue
    name = path.split("node_modules/")[-1]
    if meta.get("dev"):
        continue          # dev-only: not in the bundle a user receives
    print("%s\t%s\t%s" % (name, meta.get("version", "unknown"),
                          meta.get("license") or "UNDETERMINED"))
PYEOF
}

# ── Vendored files — committed to the repo, invisible to every package manager ─
# These are distributed by the SOURCE repository, so their obligation applies
# before any image exists. The manifest is hand-authored (there is no metadata to
# read); this enforces that it stays complete.
VENDOR_MANIFEST="vendored-licenses.tsv"

vendored_on_disk() {
  # git-tracked only: an untracked scratch file in vendor/ is not distributed.
  #
  # Licence texts are excluded. A file named OFL.txt or LICENSE.txt sitting
  # beside a vendored artifact is the NOTICE that artifact's licence requires to
  # accompany it (SIL OFL 1.1 clause 2), not a separately redistributed
  # component. Declaring one in the manifest would invent an inventory row for
  # a thing that has no licence of its own. The enforcement below still covers
  # every actual artifact, which is what it exists for.
  git ls-files 2>/dev/null \
    | grep -E '(^|/)vendor/|/onvif_wsdl/' \
    | grep -Ev '(^|/)([A-Za-z0-9._-]*-)?(OFL|LICENSE|LICENCE|COPYING)(\.[A-Za-z0-9]+)?$' \
    || true
}

vendor_components() {
  [ -f "$VENDOR_MANIFEST" ] || die "missing $VENDOR_MANIFEST — cannot account for vendored files"
  vendored_on_disk > "$WORK/vendored.txt"
  python3 - "$VENDOR_MANIFEST" "$WORK/vendored.txt" <<'PYVEND'
import sys
manifest, found = sys.argv[1], sys.argv[2]

declared, rows = [], []
for line in open(manifest):
    line = line.rstrip("\n")
    if not line.strip() or line.lstrip().startswith("#"):
        continue
    f = line.split("\t")
    if len(f) < 4:
        continue
    declared.append(f[0])
    rows.append((f[1], f[2], f[3], f[4] if len(f) > 4 else ""))

# The reverse check. Declaring a file that no longer exists is the mirror of
# the failure above and just as bad: the inventory would keep describing
# something we no longer ship. Hit for real on 2026-08-27 when the legacy SPA
# was deleted and took four vendored files with it.
import os
stale = [d for d in declared
         if not d.endswith("/") and not os.path.exists(d)]
if stale:
    for t in stale:
        print("   STALE       %s (declared, not on disk)" % t, file=sys.stderr)
    print("\n!! %d manifest entr(ies) describe files that no longer exist.\n"
          "    Remove them, or restore the files." % len(stale), file=sys.stderr)
    sys.exit(1)

undeclared = []
for path in (l.strip() for l in open(found)):
    if not path:
        continue
    # A manifest entry ending in "/" covers everything beneath it.
    if any(path == d or (d.endswith("/") and path.startswith(d)) for d in declared):
        continue
    undeclared.append(path)

if undeclared:
    for u in undeclared:
        print("   UNDECLARED  %s" % u, file=sys.stderr)
    print("\n!! %d vendored file(s) missing from %s.\n"
          "    Add each with its licence, or delete it. A file this repository\n"
          "    redistributes without a recorded licence is exactly the gap this\n"
          "    manifest exists to prevent." % (len(undeclared), manifest), file=sys.stderr)
    sys.exit(1)

print("   ok    %d vendored path(s) declared, none missing" % len(declared), file=sys.stderr)
for name, ver, lic, up in rows:
    print("%s\t%s\t%s\t%s" % (name, ver, lic, up))
PYVEND
}

# ── build ───────────────────────────────────────────────────────────────────
head_ "Preflight"
# NOT A WARNING. An inventory generated from a subset of the shipped images is
# wrong in the one direction that matters — it under-reports obligations — and
# once written it looks exactly like a complete one. Refuse instead.
missing=()
for img in "${IMAGES[@]}"; do
  if have_image "$img"; then
    say "ok    $img"
  else
    say "MISS  $img (not built here)"
    missing+=("$img")
  fi
done
if [ "${#missing[@]}" -gt 0 ]; then
  printf '\n' >&2
  say "Build them first, then re-run:"
  say "    ./vms build ${missing[*]//variphi-vms-/}"
  die "${#missing[@]} shipped image(s) not built — refusing to write a partial attribution file"
fi
assert_no_nonfree

head_ "Collecting"
: > "$WORK/all.tsv"
for img in "${IMAGES[@]}"; do
  have_image "$img" || continue
  short="${img#variphi-vms-}"
  debian_components "$img" | awk -v i="$short" -F'\t' '{print "deb\t"$1"\t"$2"\t"$3"\t"i}' >> "$WORK/all.tsv"
  python_components "$img" | awk -v i="$short" -F'\t' '{print "pip\t"$1"\t"$2"\t"$3"\t"i}' >> "$WORK/all.tsv"
  say "$short: $(grep -c "	${short}$" "$WORK/all.tsv" || true) components so far"
done
npm_components | awk -F'\t' '{print "npm\t"$1"\t"$2"\t"$3"\tspa"}' >> "$WORK/all.tsv"
vendor_components | awk -F'\t' '{print "vnd\t"$1"\t"$2"\t"$3"\trepo"}' >> "$WORK/all.tsv"
say "total rows: $(wc -l < "$WORK/all.tsv")"

head_ "Rendering $OUT"
python3 - "$WORK/all.tsv" "$WORK/rendered.md" <<'PYEOF'
import sys, collections, datetime, os
src, out = sys.argv[1], sys.argv[2]

rows = collections.OrderedDict()   # (kind,name,version) -> [licence, {images}]
for line in open(src):
    parts = line.rstrip("\n").split("\t")
    if len(parts) != 5:
        continue
    kind, name, ver, lic, img = parts
    key = (kind, name, ver)
    if key not in rows:
        rows[key] = [lic, set()]
    rows[key][1].add(img)

def family(lic):
    l = lic.upper()
    if "UNDETERMINED" in l or not lic.strip():   return "UNDETERMINED"
    if "AGPL" in l:                              return "AGPL"
    if "LGPL" in l:                              return "LGPL"
    if "GPL" in l:                               return "GPL"
    if "APACHE" in l:                            return "Apache"
    if "MIT" in l or "EXPAT" in l:               return "MIT"
    if "BSD" in l:                               return "BSD"
    if "MPL" in l:                               return "MPL"
    if "ISC" in l:                               return "ISC"
    if "PYTHON" in l or "PSF" in l:              return "PSF"
    if "PUBLIC-DOMAIN" in l or "UNLICENSE" in l: return "Public domain"
    if "PUBLIC DOMAIN" in l:                     return "Public domain"
    return "Other"

fam = collections.Counter(family(v[0]) for v in rows.values())
undet = [k for k, v in rows.items() if family(v[0]) == "UNDETERMINED"]

L = []
w = L.append
w("# Third-party notices\n")
w("**Generated file — do not edit by hand.** Regenerate with `scripts/gen-notice.sh`;")
w("`--check` fails CI when it is stale. See `NOTICE` for the copyright statement,")
w("the source-code written offer, and how to exercise it.\n")
w("This inventory is read out of the **built images**, not the manifests, because")
w("the image is what a user receives. Debian components come from each package's")
w("machine-readable `/usr/share/doc/<pkg>/copyright` (DEP-5).\n")
w("| | |")
w("|---|---|")
w("| Components | **%d** |" % len(rows))
w("| Images | %s |" % ", ".join(sorted({i for v in rows.values() for i in v[1]})))
w("| Generated by | `scripts/gen-notice.sh` |")
w("")
w("## Licence families\n")
w("| Family | Components |")
w("|---|---:|")
for k, n in fam.most_common():
    w("| %s | %d |" % (k, n))
w("")
if undet:
    w("> **%d component(s) could not be resolved automatically** and are listed as" % len(undet))
    w("> `UNDETERMINED` below. They are surfaced rather than guessed: an attribution")
    w("> file that invents a licence is worse than one that admits a gap. Each must be")
    w("> resolved by hand before publishing.\n")
w("## GPL-family components carry a source obligation\n")
w("Distributing these binaries obliges us to provide the complete corresponding")
w("source. `NOTICE` carries the written offer that discharges it. FFmpeg is the")
w("significant one: Debian builds it `--enable-gpl`, making the binaries GPL-2+")
w("(GPL-3+ for the libavcodec/libavfilter flavour). It is invoked as a subprocess,")
w("so this is aggregation and does **not** place our own code under the GPL.\n")
# EMITTED ONLY WHEN AGPL IS ACTUALLY PRESENT. The detector is deliberately one
# swappable class (services/analytics/analytics/detector.py), so a deployment
# that moves to an Apache-2.0 model should not carry a paragraph describing an
# obligation it no longer has. Driven off the inventory rather than hardcoded,
# so it appears and disappears with the dependency instead of going stale.
agpl = sorted({(n, v, lic, tuple(sorted(i)))
               for (k, n, v), (lic, i) in rows.items() if family(lic) == "AGPL"})
if agpl:
    w("## AGPL-3.0 components — the strongest obligation here\n")
    w("These are **not** ordinary GPL. AGPL-3.0 extends the source obligation to")
    w("users who interact with the software **over a network**, so operating a")
    w("modified version as a service triggers it even where nothing is distributed:\n")
    for n, v, lic, imgs in agpl:
        w("- `%s` %s — %s, in **%s**" % (n, v, lic, ", ".join(imgs)))
    w("")
    w("This is compatible with the project as it stands, because the images above")
    w("ship under AGPL-3.0 themselves. It would **not** be compatible if that")
    w("functionality moved behind a paid tier — AGPL is a one-way door. The code")
    w("keeps that door narrow on purpose: the detector is a single class behind a")
    w("`Detector` protocol, so replacing it with an Apache-2.0 model is one new")
    w("class and one line of configuration. See the header of")
    w("`services/analytics/analytics/detector.py`.\n")
w("## Full inventory\n")
w("| Component | Version | Licence | Source | In |")
w("|---|---|---|---|---|")
label = {"deb": "Debian", "pip": "Python", "npm": "npm", "vnd": "vendored in repo"}
for (kind, name, ver), (lic, imgs) in sorted(rows.items(), key=lambda kv: (kv[0][0], kv[0][1].lower())):
    lic_s = lic.replace("|", "/")
    # NEVER TRUNCATE A LICENCE TO 70 COLUMNS. A multi-licence SPDX expression is
    # long because the obligations really are plural, and "Apache-2.0 AND
    # Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND…" is an attribution
    # that stops exactly where it starts mattering. The cap that remains is a
    # safety valve against a pathological copyright file, not a layout choice —
    # nothing legitimate reaches it, because the free-text License field is
    # already dropped above once it stops looking like a name.
    if len(lic_s) > 240:
        lic_s = lic_s[:237] + "…"
    w("| `%s` | %s | %s | %s | %s |" % (name, ver, lic_s, label.get(kind, kind), ", ".join(sorted(imgs))))
w("")
w("## Vendored in the repository\n")
w("Committed directly to this repository and therefore distributed by the SOURCE")
w("release, before any image exists. No package manager reports them, so their")
w("licences come from `vendored-licenses.tsv`, whose completeness `gen-notice.sh`")
w("enforces — an undeclared file under `vendor/` fails the run.\n")
w("They appear in the inventory above marked *vendored in repo*. `UNKNOWN` in a")
w("version column means the artifact carries no determinable version and needs")
w("confirming against upstream before publishing.\n")

open(out, "w").write("\n".join(L))
print("   wrote %s — %d components, %d undetermined" % (out, len(rows), len(undet)), file=sys.stderr)
PYEOF

# Compare CONTENT, not `git diff`. An untracked THIRD-PARTY-NOTICES.md produces
# no diff, so a git-based check passes trivially on the one case that matters
# most: the file never having been committed at all.
if [ "$MODE" = "--check" ]; then
  [ -f "$OUT" ] || die "$OUT does not exist — run scripts/gen-notice.sh and commit it"
  if diff -q "$WORK/rendered.md" "$OUT" >/dev/null 2>&1; then
    say "ok    $OUT matches the images it describes"
  else
    diff -u "$OUT" "$WORK/rendered.md" | head -20 >&2 || true
    die "$OUT is stale — run scripts/gen-notice.sh and commit the result"
  fi
else
  mv "$WORK/rendered.md" "$OUT"
fi

head_ "Done"
say "review UNDETERMINED entries in $OUT before publishing"
