#!/usr/bin/env bash
# 25-migrate-data.sh [--dry-run] <node> — move an existing /srv/<node>/data onto the zvol.
#
# Run ON the node, as root, once, with the server stopped (systemctl --user stop
# <node>.service as core — stopping the container from root's podman store stops nothing).
# The `.mount` unit the deploy installed names the device (What=), so this script does not
# guess a device either.
#
# Order is the safety: copy into the mounted zvol, verify the copy, and only then take the
# old tree out of the way — to /srv/<node>/data.pre-zvol, which this script never deletes.
# If anything fails before the move the old tree is untouched; if anything after it fails,
# the old tree is still on disk under its .pre-zvol name. There is no `rm` here, and no step
# overwrites a destination that already holds something.
set -euo pipefail

DRY_RUN=0
NODE=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -*) echo "usage: ${0##*/} [--dry-run] <node>" >&2; exit 2 ;;
    *) NODE="$arg" ;;
  esac
done
[[ -n "$NODE" ]] || { echo "usage: ${0##*/} [--dry-run] <node>" >&2; exit 2; }

# Overridable only so the offline test can run this against a temp tree instead of /srv;
# production always uses the defaults.
SRV="${FLEET_SRV_ROOT:-/srv}"
UNIT_DIR="${FLEET_UNIT_DIR:-/etc/systemd/system}"
unit="srv-$NODE-data.mount"
data="$SRV/$NODE/data"
tmp="$SRV/$NODE/data.tmp"
aside="$SRV/$NODE/data.pre-zvol"

[[ -r "$UNIT_DIR/$unit" ]] \
  || { echo "$UNIT_DIR/$unit is missing — deploy $NODE first, the deploy writes the mount unit" >&2; exit 1; }
device="$(sed -n 's/^What=//p' "$UNIT_DIR/$unit")"
[[ -n "$device" ]] || { echo "$UNIT_DIR/$unit names no device" >&2; exit 1; }

mountpoint -q "$data" \
  && { echo "REFUSE: $data is already a mountpoint — there is nothing to migrate" >&2; exit 1; }
[[ -d "$data" && -n "$(ls -A "$data")" ]] \
  || { echo "REFUSE: $data is empty or missing — there is nothing to migrate onto $device" >&2; exit 1; }
[[ ! -e "$aside" ]] \
  || { echo "REFUSE: $aside already exists — a previous run left it there; move it away first" >&2; exit 1; }

echo "== ${NODE}: ${data} -> ${device}"
((DRY_RUN)) || echo "   (the server is expected to be stopped already)"
run() { echo "   \$ $*"; ((DRY_RUN)) && return 0; "$@"; }

run mkdir -p "$tmp"
run mount "$device" "$tmp"
if ((!DRY_RUN)); then
  mountpoint -q "$tmp" \
    || { echo "REFUSE: $device did not mount at $tmp — nothing copied, $data untouched" >&2; exit 1; }
  [[ -z "$(ls -A "$tmp")" ]] || {
    echo "REFUSE: $tmp already holds data — nothing copied, $data untouched" >&2
    umount "$tmp"
    exit 1
  }
fi
run cp -a "$data/." "$tmp/"
if ((!DRY_RUN)); then
  before="$(cd "$data" && find . | wc -l)"
  after="$(cd "$tmp" && find . | wc -l)"
  [[ "$before" == "$after" ]] || {
    echo "REFUSE: copied $after of $before entries — $data is untouched, $tmp holds a partial copy" >&2
    exit 1
  }
  echo "   verified: $after entries copied"
fi
run umount "$tmp"
run mv "$data" "$aside"
run mkdir -p "$data"
run systemctl start "$unit"
if ((!DRY_RUN)); then
  mountpoint -q "$data" \
    || { echo "FAIL: $data is not a mountpoint after the migration; the old tree is $aside" >&2; exit 1; }
  echo "OK: $device is $data; the old tree is $aside (moved, not deleted)"
  echo "    next: re-run the deploy for $NODE (ownership, then the container)"
fi
