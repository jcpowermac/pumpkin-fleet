#!/usr/bin/env bash
# 10-make-template.sh — scrub per-VM identity from the golden VM, then promote it to a PVE template.
# Runs on the laptop.
#
# !! CLOVING THIS TEMPLATE DOES NOT WORK. Kept only because it produces a usable reference
# !! image of a fully-baked uCore VM. Use 20-create-nodes.sh to make game nodes.
#
# Why: on a cloned disk, FCOS's coreos-gpt-setup regenerates the root filesystem UUID and
# rewrites the bootloader mid-boot, so Ignition's useradd runs against a half-mounted /sysroot
# ("useradd: group 'adm' does not exist") and the boot lands in the emergency shell. Ignition is
# one-shot per system by design and will not replay on a clone. A node must be imported from the
# qcow2 so it gets a genuine first boot.
#
# Scrub happens INSIDE the running guest: on ostree, /etc in the deployment dir
# (/ostree/deploy/<os>/deploy/<hash>.0/etc) is the immutable image copy — writes there are
# ignored. Only the mounted running system reflects reality.
#
# Identity that must be per-clone is left to the per-clone Ignition (see 20-clone-vms.sh):
# hostname, static IP. Identity that can be generated locally is reset here so each clone
# makes its own: machine-id, SSH host keys.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
set -a; source configs/pins.env; set +a

SSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 core@${IP_SPIKE}"

echo "== 1. scrub guest identity (live)"
$SSH 'sudo /bin/sh -s' <<'GUEST'
set -e
# per-boot provisioning unit has done its job and fails on every later boot
systemctl disable fleet-provision.service 2>/dev/null || true
rm -f /etc/systemd/system/multi-user.target.wants/fleet-provision.service

# static IP is per-clone (Ignition rewrites it); leaving it makes every clone fight for .99
rm -f /etc/NetworkManager/system-connections/fleet-static.nmconnection

# regenerate per clone
: > /etc/machine-id
rm -f /etc/ssh/ssh_host_* /var/lib/systemd/random-seed

# history / logs
rm -f /root/.bash_history /home/core/.bash_history /var/home/core/.bash_history
journalctl --rotate >/dev/null 2>&1 || true
journalctl --vacuum-time=1s >/dev/null 2>&1 || true
echo "scrubbed: $(ls /etc/NetworkManager/system-connections/ | tr '\n' ' ')"
GUEST

echo "== 2. stop VM and re-arm ignition.firstboot so every clone applies its own config"
ssh "$PVE_HOST" bash -s <<EOF
set -euo pipefail
VMID=${SPIKE_VMID}
qm stop \$VMID 2>/dev/null || true
sleep 2
# extboot = p3 of the FCOS GPT layout; grub's 05_ignition.cfg only passes
# ignition.firstboot when this marker file exists.
SCSI=\$(qm config \$VMID | awk -F': ' '/^scsi0:/{print \$2}' | cut -d, -f1)
DEV=/dev/zvol/\${SCSI%%:*}/\${SCSI#*:}
echo "boot disk: \$DEV"
L=\$(losetup -f)
losetup -o \$((264192*512)) --sizelimit \$((786432*512)) "\$L" "\$DEV"
mkdir -p /tmp/extboot\$VMID && mount "\$L" /tmp/extboot\$VMID
touch /tmp/extboot\$VMID/ignition.firstboot
ls /tmp/extboot\$VMID/ignition.firstboot
umount /tmp/extboot\$VMID && losetup -d "\$L"
EOF

echo "== 3. promote to template"
ssh "$PVE_HOST" "set -e; qm template ${SPIKE_VMID}; qm config ${SPIKE_VMID} | grep -E '^(template|name|ide2|cicustom)'"
echo "OK: template ${SPIKE_VMID} ready"
