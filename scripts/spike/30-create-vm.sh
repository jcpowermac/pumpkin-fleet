#!/usr/bin/env bash
# 30-create-vm.sh — (re)create the spike VM on pve3 from build/fcos-*.qcow2 + fw_cfg ignition.
# Runs on the laptop; PVE-side commands are generated into build/pve3-spike.sh and piped over SSH.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
source configs/pins.env

FCOS_QCOW2="$(ls build/fcos-${FCOS_BUILD}.qcow2 2>/dev/null || { echo "missing build/fcos-${FCOS_BUILD}.qcow2 — run 10-download-fcos.sh" >&2; exit 1; })"
[[ -s build/ignition.ign ]] || { echo "missing build/ignition.ign — run 20-build-ignition.sh" >&2; exit 1; }

# QEMU fw_cfg escaping: comma → double comma (same rule as poseidon/freeipa).
IGN_ESC="$(sed 's/,/,,/g' build/ignition.ign | tr -d '\n')"

cat > build/pve3-spike.sh <<EOF
#!/usr/bin/env bash
set -euo pipefail
VMID=${SPIKE_VMID}

if qm status "\$VMID" >/dev/null 2>&1; then
  echo "destroying existing VM \$VMID"
  qm stop "\$VMID" 2>/dev/null || true
  qm destroy "\$VMID" --force
fi

# qm importdisk creates the VM + first disk on the KVM disk storage.
qm importdisk "\$VMID" /tmp/ucore-spike-fcos-${FCOS_BUILD}.qcow2 ${PVE_VM_STORAGE}
rm -f /tmp/ucore-spike-fcos-${FCOS_BUILD}.qcow2

# importdisk defaults the disk to virtio0 on q35? Force a known, bootable layout:
qm set "\$VMID" --name ${SPIKE_NAME} --memory ${SPIKE_MEM_MB} --cores ${SPIKE_CORES} --cpu host \\
  --net0 virtio,bridge=${PVE_BRIDGE} --machine q35 --bios ovmf \\
  --agent enabled=1 --tablet 1 --vga serial0 --serial0 socket \\
  --boot order=virtio0
qm set "\$VMID" --args "-fw_cfg name=opt/com.coreos/config,string=${IGN_ESC}"
qm start "\$VMID"
echo "VM \$VMID started (disk: \$(qm config \$VMID | grep -E '^(virtio|scsi)[0-9]'))"
EOF

scp "$FCOS_QCOW2" "${PVE_HOST}:/tmp/ucore-spike-fcos-${FCOS_BUILD}.qcow2"
ssh "$PVE_HOST" bash -s < build/pve3-spike.sh
echo "OK: VM ${SPIKE_VMID} created and started (ignition via fw_cfg)"
