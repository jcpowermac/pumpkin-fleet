#!/usr/bin/env bash
# 10-download-fcos.sh [--print-url] — fetch the *pinned* FCOS build (proxmoxve qcow2) onto
# pve3, sha256-verified.
#
# The build is a pin, not a discovery: FCOS_BUILD in configs/pins.env names the release a
# rebuild imports, and that build's own meta.json carries the artifact path and its sha256
#   https://builds.coreos.fedoraproject.org/prod/streams/stable/builds/<build>/x86_64/meta.json
# Only FCOS_BUILD=auto resolves the live stream (raw.githubusercontent.com/coreos/
# fedora-coreos-streams — GitHub moved the builds off builds.coreos.org, see the ledger
# ruling in Task 1), and only that case rewrites FCOS_BUILD in configs/pins.env.
#
# --print-url resolves and prints the build id, the meta.json URL, the artifact URL and the
# sha256, then stops. It never downloads the image and never writes pins.env. Reading one
# small JSON document is how a pinned build's sha256 is known (the per-build meta.json is
# immutable for that build id, so the pin already covers it) — that is not the download the
# pin exists to avoid.
#
# Runs on the laptop; the download itself is generated into build/pve3-download.sh and piped
# to pve3 over SSH.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
source configs/pins.env

BUILDS_BASE="https://builds.coreos.fedoraproject.org/prod/streams/stable/builds"
STREAM_JSON_URL="https://raw.githubusercontent.com/coreos/fedora-coreos-streams/main/streams/stable.json"

PRINT_URL=0
for arg in "$@"; do
  case "$arg" in
    --print-url) PRINT_URL=1 ;;
    *) echo "usage: ${0##*/} [--print-url]" >&2; exit 2 ;;
  esac
done

meta_url() { echo "$BUILDS_BASE/$1/x86_64/meta.json"; }

# The live stream names the current stable build — the only case pins.env does not decide.
live_build() {
  curl -fsSL --retry 3 "$STREAM_JSON_URL" | python3 -c '
import json, sys
print(json.load(sys.stdin)["architectures"]["x86_64"]["artifacts"]["proxmoxve"]["release"])'
}

# Or, out of that build's meta.json: its proxmoxve qcow2 and the sha256 the fetch verifies.
artifact() {  # $1 = build id -> "<absolute url> <sha256>"
  local build="$1" meta
  meta="$(curl -fsSL --retry 3 "$(meta_url "$build")")" \
    || { echo "no meta.json for FCOS build $build ($(meta_url "$build"))" >&2; exit 1; }
  python3 -c '
import json, sys
image = json.load(sys.stdin)["images"]["proxmoxve"]
print(sys.argv[1] + "/" + image["path"], image["sha256"])' "$BUILDS_BASE/$build/x86_64" <<<"$meta"
}

BUILD="$FCOS_BUILD"
if [[ "$BUILD" == auto ]]; then
  BUILD="$(live_build)" \
    || { echo "could not resolve the live FCOS stable stream ($STREAM_JSON_URL)" >&2; exit 1; }
  [[ -n "$BUILD" ]] || { echo "the live FCOS stable stream named no build" >&2; exit 1; }
fi
ARTIFACT="$(artifact "$BUILD")"
read -r URL SHA256 <<<"$ARTIFACT"
[[ -n "$URL" && -n "$SHA256" ]] || { echo "could not resolve the FCOS qcow2 for $BUILD" >&2; exit 1; }

if ((PRINT_URL)); then
  echo "build:  $BUILD"
  [[ "$FCOS_BUILD" != auto ]] || echo "stream: $STREAM_JSON_URL"
  echo "meta:   $(meta_url "$BUILD")"
  echo "qcow2:  $URL"
  echo "sha256: $SHA256"
  exit 0
fi

mkdir -p build
cat > build/pve3-download.sh <<EOF
#!/usr/bin/env bash
set -euo pipefail
T=/tmp/fcos-stable
OUT=/tmp/ucore-spike-fcos-$BUILD.qcow2
mkdir -p "\$T"
if [[ -s "\$OUT" ]] && echo "$SHA256  \$OUT" | sha256sum -c --quiet; then
  echo "reusing verified \$OUT"
else
  curl -fL --retry 3 -o "\$T/proxmoxve.qcow2.xz" "$URL"
  echo "$SHA256  \$T/proxmoxve.qcow2.xz" | sha256sum -c
  xz -dk -c "\$T/proxmoxve.qcow2.xz" > "\$OUT"
fi
echo "FCOS $BUILD imported to \$OUT"
EOF

OUTPUT="$(ssh "$PVE_HOST" bash -s < build/pve3-download.sh)"
echo "$OUTPUT"
if [[ "$FCOS_BUILD" == auto ]]; then
  sed -i.bak "s/^FCOS_BUILD=.*/FCOS_BUILD=$BUILD/" configs/pins.env && rm -f configs/pins.env.bak
  echo "OK: FCOS $BUILD proxmoxve qcow2 on pve3 (/tmp/ucore-spike-fcos-$BUILD.qcow2), pinned in configs/pins.env"
else
  echo "OK: FCOS $BUILD proxmoxve qcow2 on pve3 (/tmp/ucore-spike-fcos-$BUILD.qcow2), pins.env unchanged (FCOS_BUILD=$BUILD was already pinned)"
fi
