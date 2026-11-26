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
# rather than duplicating it here. Render-only skips it: that read is a pve3 call.
if ((RENDER_ONLY)); then
  SSSD_BIND_USER=""; SSSD_BIND_AUTHTOK=""
else
  SSSD_BIND="$(ssh "$PVE_HOST" pct exec 150 -- cut -d' ' -f1,2 /root/ldap-user-password | tr -d '\n')"
  SSSD_BIND_USER="${SSSD_BIND%% *}"
  SSSD_BIND_AUTHTOK="${SSSD_BIND##* }"
  [[ "$SSSD_BIND_USER" != "$SSSD_BIND_AUTHTOK" ]] || { echo "SSSD bind credential not parsed" >&2; exit 1; }
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

mkdir -p build/nodes
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
  echo "OK: ${#NODES[@]} nodes created; each rebases on first boot (~5-10 min)"
fi
