#!/usr/bin/env bash
# 60-make-template.sh — scrub per-VM state and promote the spike VM to PVE template 190.
# Runs on the laptop. Prereq: 50-verify.sh passed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
source configs/pins.env

ssh "$PVE_HOST" "
set -euo pipefail
VMID=${SPIKE_VMID}
if qm config \$VMID | grep -q '^template: 1'; then echo 'already a template'; exit 0; fi
qm stop \$VMID
DISK=\$(qm config \$VMID | awk -F: '/^(virtio|scsi)[0-9]+:/{print \$1; exit}')
echo \"first disk: \$DISK\"
MNT=\$(qm mount \$VMID \$DISK | awk '{print \$NF}' | tr -d \"'\")
echo \"mounted at: \$MNT\"
rm -f \"\$MNT/etc/machine-id\"
: > \"\$MNT/var/lib/systemd/random-seed\" 2>/dev/null || true
qm unmount \$VMID
qm set \$VMID --name ucore-golden
qm template \$VMID
qm config \$VMID | head -5
"
echo "OK: template ${SPIKE_VMID} (ucore-golden) ready for cloning"
