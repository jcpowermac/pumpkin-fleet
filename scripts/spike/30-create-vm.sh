#!/usr/bin/env bash
# 30-create-vm.sh — (re)create the spike VM on pve3 from the pve3-local FCOS qcow2.
# Runs on the laptop; PVE-side commands are generated into build/pve3-spike.sh and piped over SSH.
#
# Follows https://docs.fedoraproject.org/en-US/fedora-coreos/provisioning-proxmoxve/ exactly.
# Do NOT "improve" this back to OVMF + fw_cfg: the FCOS proxmoxve platform fetches its
# Ignition config from the cloud-init cidata drive (`--ide2 STORAGE:cloudinit` +
# `--cicustom vendor=STORAGE:snippets/<ign>`). Without that drive Ignition logs
# "timed out while fetching config from config drive (cidata)" and applies nothing.
# UEFI/OVMF is not used by the upstream recipe and only adds a temp-efivars hazard.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
source configs/pins.env

[[ -s build/ignition.ign ]] || { echo "missing build/ignition.ign — run 20-build-ignition.sh" >&2; exit 1; }

cat > build/pve3-spike.sh <<EOF
#!/usr/bin/env bash
set -euo pipefail
VMID=${SPIKE_VMID}
IMG=/tmp/ucore-spike-fcos-${FCOS_BUILD}.qcow2
STORAGE=${PVE_VM_STORAGE}
ICONF=/var/coreos/snippets/fleet.ign
[[ -s "\$IMG" ]] || { echo "missing \$IMG — run 10-download-fcos.sh" >&2; exit 1; }

# Storage for snippets + images. `content` is the property; a bare \`snippets 1\` line
# makes PVE reject the whole stanza ("unable to parse value of 'snippets'").
mkdir -p /var/coreos/snippets /var/coreos/images
pvesm status 2>/dev/null | grep -q '^fleet ' || pvesm add dir fleet --path /var/coreos --content images,snippets
install -m 0644 /tmp/fleet.ign "\$ICONF"

if qm status "\$VMID" >/dev/null 2>&1; then
  echo "destroying existing VM \$VMID"
  qm stop "\$VMID" 2>/dev/null || true
  qm destroy "\$VMID" --purge
fi

qm create "\$VMID" --name ${SPIKE_NAME} --memory ${SPIKE_MEM_MB} --cores ${SPIKE_CORES} --cpu host \\
  --net0 virtio,bridge=${PVE_BRIDGE} --scsihw virtio-scsi-pci \\
  --agent enabled=1 --tablet 1 --vga serial0 --serial0 socket

# PVE 9: single-step import via scsi0=import-from (the old `qm set scsi0=...,import=` is gone)
qm set "\$VMID" --scsi0 "\${STORAGE}:0,import-from=\$IMG"
qm resize "\$VMID" scsi0 ${SPIKE_DISK_GB}G

qm set "\$VMID" --ide2 "\${STORAGE}:cloudinit"
qm set "\$VMID" --boot order=scsi0
qm set "\$VMID" --cicustom "vendor=fleet:snippets/fleet.ign"
qm set "\$VMID" --ciupgrade 0
qm set "\$VMID" --ipconfig0 "ip=${SPIKE_IP4},gw=${FLEET_GW}"

qm start "\$VMID"
echo "VM \$VMID started"
EOF

scp -q build/ignition.ign "${PVE_HOST}:/tmp/fleet.ign"
ssh "$PVE_HOST" bash -s < build/pve3-spike.sh
echo "OK: VM ${SPIKE_VMID} created and started (ignition via cloud-init cidata)"
