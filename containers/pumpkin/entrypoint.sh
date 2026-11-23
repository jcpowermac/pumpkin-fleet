#!/bin/sh
set -eu
mkdir -p /data/plugins
# Seed bundled runtime plugins once; never overwrite what is already on the volume.
for f in /opt/bundled-plugins/*; do
  [ -e "$f" ] || continue
  b=$(basename "$f")
  [ -e "/data/plugins/$b" ] || cp -p "$f" "/data/plugins/$b"
done
exec /opt/pumpkin "$@"
