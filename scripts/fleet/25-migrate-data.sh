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
# NODE becomes both a path component and a unit name, so anything else is a mistake, not a
# node: `..` would walk out of /srv and name a unit that cannot exist.
case "$NODE" in
  lobby|plugins|vanilla|neoforge) ;;
  *) echo "usage: ${0##*/} [--dry-run] <node>   (node is one of: lobby plugins vanilla neoforge)" >&2; exit 2 ;;
esac

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

# A running container turns every later step into a lie: `cp -a` snapshots a world that is
# still being written, `diff -r` then passes against that snapshot, and the `mv` moves the
# directory out from under the live bind mount — the next deploy starts on the older zvol
# copy and nothing says so. The unit is core's (rootless), so ask core's user manager.
STOP="sudo -u core env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user"
# shellcheck disable=SC2086  # a command prefix, split on purpose
status=0; $STOP is-active --quiet "$NODE.service" || status=$?
case "$status" in
  0) echo "REFUSE: $NODE.service is running — a copy of a live world is a snapshot, and the mv would move it out from under the container." >&2
     echo "        stop it first: $STOP stop $NODE.service" >&2
     exit 1 ;;
  3) : ;;   # inactive (or no such unit): what a node being migrated looks like
  *) echo "REFUSE: cannot tell whether $NODE.service is running (systemctl exited $status) — not copying a world blind" >&2
     exit 1 ;;
esac

echo "== ${NODE}: ${data} -> ${device}"
((DRY_RUN)) || echo "   (the server is expected to be stopped already)"
run() { echo "   \$ $*"; ((DRY_RUN)) && return 0; "$@"; }

run mkdir -p "$tmp"
# Guarded, because `set -e` would otherwise kill the script at `mount` with an empty exit
# status and the refusal below would never be printed — "wrong fs type" on a zvol that no
# deploy ever formatted is exactly how that happens.
run mount "$device" "$tmp" \
  || { echo "REFUSE: $device did not mount at $tmp — nothing copied, $data untouched." >&2
       echo "        An unformatted zvol means the deploy never reached its format step: run the deploy for $NODE once (it formats a blank device, then refuses this tree), then retry." >&2
       exit 1; }
if ((!DRY_RUN)); then
  mountpoint -q "$tmp" \
    || { echo "REFUSE: $device did not mount at $tmp — nothing copied, $data untouched" >&2; exit 1; }
  [[ -z "$(ls -A "$tmp")" ]] || {
    echo "REFUSE: $device already holds data — nothing copied, $data is untouched." >&2
    echo "        It may hold a partial copy from an aborted run of this script; $data is still the world." >&2
    echo "        Check it, then empty it by hand if that is what it is: umount $tmp; mount $device $tmp" >&2
    umount "$tmp"
    exit 1
  }
fi
run cp -a "$data/." "$tmp/"
# `diff -r` compares what is there, byte for byte; equal entry counts do not mean a faithful
# copy. --no-dereference because `cp -a` preserves symlinks while plain `diff -r` follows
# them: a faithful copy containing a symlink would be refused as different.
# Guarded so a difference unmounts the zvol before exiting — otherwise the next run refuses
# with "already holds data" and hides the real reason.
echo "   \$ diff -r --no-dereference $data $tmp"
if ((!DRY_RUN)) && ! diff -r --no-dereference "$data" "$tmp" >&2; then
  echo "REFUSE: $tmp differs from $data — nothing moved and $data is untouched (unmounting $tmp)" >&2
  umount "$tmp"
  exit 1
fi
((DRY_RUN)) || echo "   verified: $tmp matches $data"
run umount "$tmp"
run mv "$data" "$aside"
run mkdir -p "$data"
# The deploy wrote this unit and then refused, so systemd has never read it: `start` without
# a reload fails with "Unit ... not found" — after the tree has already moved, which makes
# the failure look like a broken mount instead of a missing daemon-reload.
run systemctl daemon-reload
# Named failure: after the move `set -e` would otherwise end the script with an empty
# /srv/<node>/data and no word about where the world is.
echo "   \$ systemctl start $unit"
if ((!DRY_RUN)) && ! systemctl start "$unit"; then
  echo "FAIL: $unit did not mount $device at $data — the old tree is $aside (moved, not deleted)" >&2
  exit 1
fi
if ((!DRY_RUN)); then
  mountpoint -q "$data" \
    || { echo "FAIL: $data is not a mountpoint after the migration; the old tree is $aside" >&2; exit 1; }
  echo "OK: $device is $data; the old tree is $aside (moved, not deleted)"
  echo "    next: re-run the deploy for $NODE (ownership, then the container)"
fi
