#!/usr/bin/env bash
# 50-verify.sh — spike acceptance: uCore booted, static IP, SSH, guest agent, rollback available.
# Fast, non-destructive: does not reboot. Pass --rollback to also exercise the rollback cycle
# (two guest reboots, ~4 min).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
set -a; source configs/pins.env; set +a

SSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 core@${IP_SPIKE}"
PASS=0; FAIL=0
ok()  { echo "PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "FAIL: $1"; FAIL=$((FAIL+1)); }
wait_ssh() { for i in $(seq 1 60); do $SSH 'true' 2>/dev/null && return 0; sleep 5; done; return 1; }

# 1. uCore is the booted deployment.
BOOTED="$($SSH 'rpm-ostree status --booted | sed -n "s/^● *//p"')"
[[ "$BOOTED" == *ublue-os/ucore* ]] && ok "booted deployment: $BOOTED" || bad "booted deployment ($BOOTED)"
$SSH 'grep ^PRETTY_NAME /etc/os-release'

# 2. Static IP survives reboot (the nmcli connection lives in the deployment's /var).
GOT="$($SSH 'ip -4 -o addr show scope global | awk "{print \$4}" | head -1')"
[[ "$GOT" == "${SPIKE_IP4}" ]] && ok "static IP ${GOT}" || bad "static IP (want ${SPIKE_IP4}, got '${GOT:-none}')"

# 3. Guest agent answers from the host (needs `agent: enabled=1` + a cold QEMU start).
if ssh "$PVE_HOST" "qm agent ${SPIKE_VMID} ping" >/dev/null 2>&1; then
  ok "qemu-guest-agent responds"
else
  bad "qemu-guest-agent (VM needs a cold start for the virtio-serial port)"
fi

# 4. Rollback target exists.
COUNT="$($SSH 'rpm-ostree status | grep -cE "^[● ] ostree-"')"
(( COUNT >= 2 )) && ok "${COUNT} deployments present (rollback available)" || bad "rollback target missing (${COUNT} deployment)"

# 5. Cockpit is NOT in the uCore base image — carried as a known gap, layered separately.
if $SSH 'rpm -q cockpit' >/dev/null 2>&1; then
  ok "cockpit installed"
else
  echo "NOTE: cockpit not installed (not in the uCore base image; layer it with rpm-ostree if needed)"
fi

if [[ "${1:-}" == "--rollback" ]]; then
  BEFORE="$BOOTED"
  echo "== rollback test (this reboots the VM twice)"
  $SSH 'sudo rpm-ostree rollback --reboot' || true
  wait_ssh || bad "SSH after rollback"
  MID="$($SSH 'rpm-ostree status --booted | sed -n "s/^● *//p"')"
  [[ "$MID" != "$BEFORE" ]] && ok "rolled back to: $MID" || bad "rollback did not flip deployment"
  $SSH 'sudo rpm-ostree rollback --reboot' || true
  wait_ssh || bad "SSH after restore"
  AFTER="$($SSH 'rpm-ostree status --booted | sed -n "s/^● *//p"')"
  [[ "$AFTER" == "$BEFORE" ]] && ok "restored to: $AFTER" || bad "restore failed (after=$AFTER)"
fi

echo "----"
echo "spike verify: ${PASS} passed, ${FAIL} failed"
[[ $FAIL -eq 0 ]]
