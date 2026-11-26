#!/usr/bin/env bash
# 20-create-nodes.sh — create the four game nodes, one fresh FCOS VM each, with its own Ignition.
# Runs on the laptop.
#
# WHY NOT CLONE THE GOLDEN TEMPLATE: on a cloned disk, FCOS's coreos-gpt-setup regenerates the
# root filesystem UUID and rewrites the bootloader mid-boot, so Ignition's useradd runs against a
# half-mounted /sysroot ("group 'adm' does not exist") and the boot falls into the emergency
# shell. Ignition is explicitly one-shot per system. Re-importing the image gives every node a
# genuine first boot, which is the documented path and the one that already works.
#
# Each node's Ignition does: static IP -> uCore rebase (--bypass-driver) -> reboot.
#
# --render-only: render every node's butane/Ignition into build/nodes/ and stop before any
# pve3 contact — no ssh/scp, no qm, and no sssd bind credential fetch. That render is for
# review/diff only (its sssd bind fields come out empty), which is what makes a node rebuild
# reviewable without a host key. tests/test_create_nodes_render.sh proves the offline half.
#
# ORDER MATTERS, and this script is the last step, not the first: the destroy below takes
# the whole VM including its scsi0, and a world that has not been moved to the host-owned
# zvol yet goes with it. On node 201 that is the situation today (`scsi0` is
# pool:vm-201-disk-0 and the world is on it). The order is now ENFORCED, not advised: the
# read-only pre-flight before the loop refuses the whole run if any node's scsi1 is not its
# host-owned zvol, or if the world is not *proven* to be on that zvol already. Run, in this
# order:
#
#   1. scripts/fleet/15-ensure-data-disks.sh       create pool/mcdata/<node> and attach
#                                                  scsi1 to the node that is STILL RUNNING
#   2. ssh core@<ip> ls /dev/disk/by-id            read the name the guest sees for scsi1, then
#      configs/pins.env: FLEET_DATA_DEV=/dev/disk/by-id/<entry>    set the pin to it
#   3. pyinfra … tasks/gameserver.py               writes the .mount unit, formats the blank
#                                                  zvol, then refuses a /srv/<node>/data
#                                                  that is not mounted yet
#   4. scripts/fleet/25-migrate-data.sh <node>     on the node: copy the old tree onto the
#                                                  zvol, then move it to data.pre-zvol
#   5. pyinfra … tasks/gameserver.py               mounts it, ownership, container starts
#   6. THIS SCRIPT                                 only now may a VM be destroyed
#   7. pyinfra … --group game_nodes                after the rebase, for all four
#
# Step 2 is not style: a host zvol's guest name is slot-derived (…HARDDISK_drive-scsi1), which
# no node-name match can recognise, so without the pin step 3 aborts before it writes the
# mount unit and step 4 then refuses with "deploy first" — the "expected refusal" step 3
# promises is never reached. One pin covers the fleet: scsi1 is the world disk on every node.
#
# If the freshly hot-plugged scsi1 is invisible to the guest, step 3's format op fails on
# `test -b`: `qm shutdown <vmid> && qm start <vmid>`, then re-run it. `qm destroy` is never
# the fix for an invisible disk — it is the one command here that can lose the world.
#
# Steps 1-3 and 5 are idempotent, so repeat them freely; step 4 refuses when the old tree is
# empty, the zvol already holds data, or the node's container is still running; step 6 is the
# only irreversible one.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
set -a; source configs/pins.env; set +a

RENDER_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --render-only) RENDER_ONLY=1 ;;
    *) echo "usage: ${0##*/} [--render-only]" >&2; exit 2 ;;
  esac
done

SSHKEY="$(cat "$CORE_SSH_PUB_KEY_FILE")"
command -v butane >/dev/null || { echo "butane not found" >&2; exit 1; }

# sssd bind credential lives in CT 150 (/root/ldap-user-password, "<user> <pass>"); fetch it
# rather than duplicating it here. Render-only skips it: that read is a pve3 call. A node
# booted with an empty bind dn joins no domain and cockpit/sssd fail at login, so an
# unreadable 150 stops the rebuild here instead of producing four of them.
if ((RENDER_ONLY)); then
  SSSD_BIND_USER=""; SSSD_BIND_AUTHTOK=""
else
  SSSD_BIND="$(ssh "$PVE_HOST" pct exec 150 -- cut -d' ' -f1,2 /root/ldap-user-password | tr -d '\n')" \
    || { echo "authentik LXC 150 unreadable — sssd bind credential unavailable" >&2; exit 1; }
  SSSD_BIND_USER="${SSSD_BIND%% *}"
  SSSD_BIND_AUTHTOK="${SSSD_BIND##* }"
  [[ -n "$SSSD_BIND_USER" && "$SSSD_BIND_USER" != "$SSSD_BIND_AUTHTOK" ]] \
    || { echo "authentik LXC 150 unreadable — sssd bind credential unavailable" >&2; exit 1; }
fi

NODES=(
  "${VMID_LOBBY}:lobby:${IP_LOBBY}"
  "${VMID_PLUGINS}:plugins:${IP_PLUGINS}"
  "${VMID_VANILLA}:vanilla:${IP_VANILLA}"
  "${VMID_NEOFORGE}:neoforge:${IP_NEOFORGE}"
)

# CANARY_NODE is a name matched against this list: a typo or a rename would otherwise leave the
# canary branch below unreached and all four nodes on the promoted channel, green. Checked before
# the loop so it fails before any node is destroyed/recreated.
for entry in "${NODES[@]}"; do
  IFS=: read -r _ name _ <<<"$entry"
  [[ "$name" != "$CANARY_NODE" ]] || canary_matched=1
done
[[ -n "${canary_matched:-}" ]] \
  || { echo "CANARY_NODE=$CANARY_NODE matches no node in this list (lobby plugins vanilla neoforge)" >&2; exit 1; }

# The decision pass: is every world already on its host-owned zvol? Read-only (`qm status`,
# `qm config`, and one node-side probe) and every node decided before any VM is touched — a
# check inside the loop would destroy 201 and only then discover that 203's world never left
# its OS disk. A node with no VM has nothing to lose; a node whose scsi1 is not
# /dev/zvol/pool/mcdata/<node> still has its world on the disposable scsi0.
#
# A zvol *attached* is not a world *migrated*: 15-ensure-data-disks.sh attaches a bare volume
# and copies nothing, so the two tests are separate. The proof for the second one lives on the
# node — /srv/<node>/data is a mountpoint, or a finished 25-migrate-data.sh left
# data.pre-zvol behind (its mount step not run yet). Anything else, including a node that does
# not answer, is unproven and refuses.
order_hint() {
  echo "        Move each world onto its zvol first, one node at a time, in this order:" >&2
  echo "          1. scripts/fleet/15-ensure-data-disks.sh" >&2
  echo "          2. ssh core@<ip> ls /dev/disk/by-id   # set FLEET_DATA_DEV in configs/pins.env to its scsi1 entry" >&2
  echo "          3. uv run pyinfra inventory/hosts.py --limit <node> tasks/gameserver.py  # writes the .mount unit, then refuses" >&2
  echo "          4. scripts/fleet/25-migrate-data.sh <node>                              # on the node, server stopped" >&2
  echo "          5. uv run pyinfra inventory/hosts.py --limit <node> tasks/gameserver.py  # mounts it, starts the container" >&2
}
if ((!RENDER_ONLY)); then
  unsafe=(); unproven=()
  for entry in "${NODES[@]}"; do
    IFS=: read -r vmid name ip <<<"$entry"
    ssh "$PVE_HOST" qm status "$vmid" >/dev/null 2>&1 || continue
    config="$(ssh "$PVE_HOST" qm config "$vmid")" \
      || { echo "REFUSE: cannot read VM $vmid (${name}) — not destroying what I cannot inspect" >&2; exit 1; }
    scsi1="$(sed -n 's/^scsi1: //p' <<<"$config")"
    # `qm config` prints the volume as it was attached plus its options: <backing>,size=...
    backing="${scsi1%%,*}"
    [[ "$backing" == "/dev/zvol/pool/mcdata/${name}" ]] || { unsafe+=("${vmid} (${name})"); continue; }
    # BatchMode: an unanswerable probe must refuse, not sit at a password prompt.
    ssh -o BatchMode=yes "core@${ip}" \
      "mountpoint -q /srv/${name}/data || test -d /srv/${name}/data.pre-zvol" >/dev/null 2>&1 \
      || unproven+=("${vmid} (${name})")
  done
  if ((${#unsafe[@]})); then
    echo "REFUSE: these VMs have no host-owned data disk attached as scsi1, so destroying" >&2
    echo "        them takes the world on their OS disk (scsi0) with it:" >&2
    for entry in ${unsafe[@]+"${unsafe[@]}"}; do echo "          VM $entry" >&2; done
    order_hint
    echo "        Nothing was stopped, detached or destroyed." >&2
    exit 1
  fi
  if ((${#unproven[@]})); then
    echo "REFUSE: these VMs have their zvol attached, but nothing proves the world is on it," >&2
    echo "        so destroying them takes the world still sitting on scsi0:" >&2
    for entry in ${unproven[@]+"${unproven[@]}"}; do echo "          VM $entry" >&2; done
    echo "        Proven means: /srv/<node>/data is a mountpoint, or a finished" >&2
    echo "        25-migrate-data.sh left /srv/<node>/data.pre-zvol behind. This check says" >&2
    echo "        nothing about *which* device is mounted — the deploy's findmnt check does." >&2
    order_hint
    echo "        A node that does not answer ssh also lands here: bring it up and re-run." >&2
    echo "        Nothing was stopped, detached or destroyed." >&2
    exit 1
  fi
fi

mkdir -p build/nodes
# The snippet dir is spike-era state no tracked step created: on a fresh host every
# `cicustom` below would point at a file that is not there. After the CANARY_NODE check,
# so a typo still fails before anything is written to the host.
((RENDER_ONLY)) || ssh "$PVE_HOST" mkdir -p /var/coreos/snippets
for entry in "${NODES[@]}"; do
  IFS=: read -r vmid name ip <<<"$entry"
  # The OS channel, per node: the canary node (by name) follows the canary tag BlueBuild
  # publishes, every other node the promoted one.
  if [[ "$name" == "$CANARY_NODE" ]]; then os_tag="$OS_TAG_CANARY"; else os_tag="$OS_TAG"; fi
  echo "== ${vmid} ${name} ${ip} ${OS_IMAGE}:${os_tag}"

  SPIKE_HOSTNAME="$name" SPIKE_IP4="$ip/24" FLEET_GW="$FLEET_GW" \
  UCORE_IMAGE="$OS_IMAGE" UCORE_TAG="$os_tag" CORE_SSH_PUB_KEY="$SSHKEY" \
  IP_AUTHENTIK="$IP_AUTHENTIK" SSSD_BIND_USER="$SSSD_BIND_USER" SSSD_BIND_AUTHTOK="$SSSD_BIND_AUTHTOK" \
  envsubst '${SPIKE_HOSTNAME} ${SPIKE_IP4} ${FLEET_GW} ${UCORE_IMAGE} ${UCORE_TAG} ${CORE_SSH_PUB_KEY} ${IP_AUTHENTIK} ${SSSD_BIND_USER} ${SSSD_BIND_AUTHTOK}' \
    < templates/fleet.ign.butane > "build/nodes/${name}.butane"
  butane -r "build/nodes/${name}.butane" > "build/nodes/${name}.ign"
  python3 -m json.tool "build/nodes/${name}.ign" >/dev/null
  if ((RENDER_ONLY)); then continue; fi
  scp -q "build/nodes/${name}.ign" "${PVE_HOST}:/var/coreos/snippets/${name}.ign"

  ssh "$PVE_HOST" bash -s <<EOF
set -euo pipefail
VMID=$vmid
IMG=/tmp/ucore-spike-fcos-${FCOS_BUILD}.qcow2
[[ -s "\$IMG" ]] || { echo "missing \$IMG" >&2; exit 1; }
if qm status \$VMID >/dev/null 2>&1; then
  echo "   \$VMID exists — destroying"
  qm stop \$VMID 2>/dev/null || true
  # --purge takes what PVE created (scsi0 in pool). The world is scsi1, a zvol the host
  # owns (pool/mcdata/${name}), so it survives qm destroy --purge and 15-ensure-data-disks.sh
  # reattaches it — on the next full run if this one aborts mid-loop — verified on pve3 with
  # a throwaway VMID
  # (docs/superpowers/specs/2026-11-26-fleet-plugin-gitops-design.md §2b).
  qm destroy \$VMID --purge
fi
qm create \$VMID --name ${name} --cores ${SPIKE_CORES} --memory ${SPIKE_MEM_MB} --cpu host \\
  --net0 virtio,bridge=${PVE_BRIDGE} --scsihw virtio-scsi-pci \\
  --agent enabled=1 --tablet 1 --vga serial0 --serial0 socket
qm set \$VMID --scsi0 "${PVE_VM_STORAGE}:0,import-from=\$IMG"
qm resize \$VMID scsi0 ${SPIKE_DISK_GB}G
qm set \$VMID --ide2 ${PVE_VM_STORAGE}:cloudinit
qm set \$VMID --boot order=scsi0
qm set \$VMID --cicustom vendor=fleet:snippets/${name}.ign
qm set \$VMID --ciupgrade 0
qm set \$VMID --ipconfig0 ip=${ip}/24,gw=${FLEET_GW}
qm start \$VMID
echo "   \$VMID started"
EOF
done

if ((RENDER_ONLY)); then
  echo "OK: ${#NODES[@]} nodes rendered to build/nodes/ (no pve3 contact, no sssd bind credential)"
else
  # After every VM exists: a world already on pool/mcdata/<node> comes back with the newly
  # imported OS disk. On a mid-loop abort this never runs, so a rebuilt node has no scsi1
  # until the next full run (the pre-flight above refuses until then). A re-run also refuses
  # until step 5's deploy has mounted the volume on every rebuilt node — that mount is the
  # proof the pre-flight asks for, and a node still booting cannot give it.
  bash "$ROOT/scripts/fleet/15-ensure-data-disks.sh"
  echo "OK: ${#NODES[@]} nodes created; each rebases on first boot (~5-10 min)"
fi
