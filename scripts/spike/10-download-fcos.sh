#!/usr/bin/env bash
# 10-download-fcos.sh — download the pinned FCOS QEMU image (idempotent).
# Runs on the laptop. Writes FCOS_BUILD into configs/pins.env.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
source configs/pins.env

BUILD_DIR="$ROOT/build"
mkdir -p "$BUILD_DIR"

# Resolve the current stable build id + qcow2 URL.
STABLE_JSON="$(curl -fsSL https://builds.coreos.org/prod/streams/stable.json)"
FCOS_BUILD="$(python3 -c "import sys,json;print(json.loads(sys.argv[1])['metadata']['build_id'])" "$STABLE_JSON")"
QEMU_URL="$(python3 -c "import sys,json;print(json.loads(sys.argv[1])['builds']['x86_64']['qemu']['qcow2']['url'])" "$STABLE_JSON")"
echo "FCOS stable build: $FCOS_BUILD"

OUT="$BUILD_DIR/fcos-$FCOS_BUILD.qcow2"
if [[ -s "$OUT" ]] && [[ -f "$OUT.sha256" ]]; then
  echo "Reusing existing image: $OUT"
else
  echo "Downloading $QEMU_URL"
  curl -fL --retry 3 -o "$BUILD_DIR/fcos.qcow2.xz" "$QEMU_URL"
  xz -dk "$BUILD_DIR/fcos.qcow2.xz"
  mv "$BUILD_DIR/fcos.qcow2" "$OUT"
  ( cd "$BUILD_DIR" && sha256sum "$(basename "$OUT")" > "$OUT.sha256" )
fi
( cd "$BUILD_DIR" && sha256sum -c "$(basename "$OUT").sha256" )

# Pin the build id (idempotent rewrite).
sed -i.bak "s/^FCOS_BUILD=.*/FCOS_BUILD=$FCOS_BUILD/" configs/pins.env && rm -f configs/pins.env.bak
echo "OK: $OUT (build $FCOS_BUILD, pinned in configs/pins.env)"
