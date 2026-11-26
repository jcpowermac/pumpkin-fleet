#!/usr/bin/env bash
# 15-ensure-data-disks.sh [--dry-run] — give every game node its world disk.
#
# The world lives on a ZFS volume the PVE host owns, pool/mcdata/<node>, attached to the VM
# as a raw device (scsi1). That is what makes a rebuild safe: `qm destroy --purge` removes
# what PVE created (scsi0, in `pool`), and not a path PVE did not create — verified on pve3
# with a throwaway VMID: created pool/test-mcdata, attached it as /dev/zvol/..., destroyed
# the VM with --purge, and the zvol was still there
# (docs/superpowers/specs/2026-11-26-fleet-plugin-gitops-design.md §2b).
#
# This script cannot lose a world. It never destroys a zvol, never detaches scsi1, and
# refuses when a VM's scsi1 is already backed by something else — that disk may hold
# someone's world and detaching it is not a rebuild step. Every node is decided before any
# node is acted on, so one unsafe VM stops the run with nothing created or attached.
# --dry-run decides the same way, prints the commands and runs none of them.
#
# Runs on the laptop. Reads pve3 over ssh; the only writes are `zfs create` for a missing
# zvol and `qm set --scsi1` for a VM that has no scsi1 yet.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
source configs/pins.env

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    *) echo "usage: ${0##*/} [--dry-run]" >&2; exit 2 ;;
  esac
done

NODES=(
  "${VMID_LOBBY}:lobby"
  "${VMID_PLUGINS}:plugins"
  "${VMID_VANILLA}:vanilla"
  "${VMID_NEOFORGE}:neoforge"
)

actions=()
for entry in "${NODES[@]}"; do
  IFS=: read -r vmid node <<<"$entry"
  zvol="pool/mcdata/$node"
  dev="/dev/zvol/$zvol"
  echo "== ${vmid} ${node} ${dev}"

  if ssh "$PVE_HOST" zfs list -H -o name "$zvol" >/dev/null 2>&1; then
    echo "   zvol exists — not touching it"
  else
    echo "   zvol missing — needs creating (${MC_DATA_DISK_GB}G)"
    actions+=("zfs create -V ${MC_DATA_DISK_GB}G $zvol")
  fi

  config="$(ssh "$PVE_HOST" qm config "$vmid")" \
    || { echo "VM $vmid is not on $PVE_HOST — nothing attached, nothing created" >&2; exit 1; }
  scsi1="$(sed -n 's/^scsi1: //p' <<<"$config")"
  if [[ -z "$scsi1" ]]; then
    echo "   scsi1 absent — needs attaching"
    actions+=("qm set $vmid --scsi1 $dev")
  else
    # `qm config` prints the volume as it was attached plus its options: <backing>,size=...
    backing="${scsi1%%,*}"
    [[ "$backing" == "$dev" ]] || {
      echo "REFUSE: VM ${vmid} (${node}) already has scsi1 backed by '${backing}', not '${dev}'." >&2
      echo "        That disk is in use; detaching or overwriting it is a human decision, not a rebuild step." >&2
      exit 1
    }
    echo "   scsi1 already ${dev} — not touching it"
  fi
done

if ((DRY_RUN)); then
  echo "-- dry run: nothing created, nothing attached, would run:"
  for action in ${actions[@]+"${actions[@]}"}; do echo "   \$ $action"; done
  exit 0
fi
# bash 3.2 (macOS) treats an empty "${a[@]}" as unbound under set -u; the + guard is the
# portable spelling of "zero or more".
for action in ${actions[@]+"${actions[@]}"}; do
  echo "\$ $action"
  # shellcheck disable=SC2086  # the action is a command line, split on purpose
  ssh "$PVE_HOST" $action
done
if ((${#actions[@]} == 0)); then
  echo "OK: every node already has pool/mcdata/<node> attached as scsi1"
else
  echo "OK: applied ${#actions[@]} action(s)"
fi
