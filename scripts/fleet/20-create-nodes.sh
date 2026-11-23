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
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
set -a; source configs/pins.env; set +a

SSHKEY="$(cat "$CORE_SSH_PUB_KEY_FILE")"
command -v butane >/dev/null || { echo "butane not found" >&2; exit 1; }

NODES=(
  "${VMID_LOBBY}:lobby:${IP_LOBBY}"
  "${VMID_PLUGINS}:plugins:${IP_PLUGINS}"
  "${VMID_VANILLA}:vanilla:${IP_VANILLA}"
  "${VMID_NEOFORGE}:neoforge:${IP_NEOFORGE}"
)

mkdir -p build/nodes
for entry in "${NODES[@]}"; do
  IFS=: read -r vmid name ip <<<"$entry"
  echo "== ${vmid} ${name} ${ip}"

  SPIKE_HOSTNAME="$name" SPIKE_IP4="$ip/24" FLEET_GW="$FLEET_GW" \
  UCORE_IMAGE="$UCORE_IMAGE" UCORE_TAG="$UCORE_TAG" CORE_SSH_PUB_KEY="$SSHKEY" \
  envsubst '${SPIKE_HOSTNAME} ${SPIKE_IP4} ${FLEET_GW} ${UCORE_IMAGE} ${UCORE_TAG} ${CORE_SSH_PUB_KEY}' \
    < templates/fleet.ign.butane > "build/nodes/${name}.butane"
  butane -r "build/nodes/${name}.butane" > "build/nodes/${name}.ign"
  python3 -m json.tool "build/nodes/${name}.ign" >/dev/null
  scp -q "build/nodes/${name}.ign" "${PVE_HOST}:/var/coreos/snippets/${name}.ign"

  ssh "$PVE_HOST" bash -s <<EOF
set -euo pipefail
VMID=$vmid
IMG=/tmp/ucore-spike-fcos-${FCOS_BUILD}.qcow2
[[ -s "\$IMG" ]] || { echo "missing \$IMG" >&2; exit 1; }
if qm status \$VMID >/dev/null 2>&1; then
  echo "   \$VMID exists — destroying"
  qm stop \$VMID 2>/dev/null || true
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

echo "OK: ${#NODES[@]} nodes created; each rebases on first boot (~5-10 min)"
