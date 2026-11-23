#!/usr/bin/env bash
# 40-wait-rebase.sh — wait until the spike VM is up and running the uCore deployment.
# State-driven: polls SSH + `rpm-ostree status`, reports the real deployment each tick.
# The rebase pull is the slow part (~5-10 min on a cold ghcr.io cache).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
set -a; source configs/pins.env; set +a

IP="${SPIKE_IP4%%/*}"
MAX_SECS=${WAIT_MAX_SECS:-1200}
INTERVAL=${WAIT_INTERVAL:-15}
start=$(date +%s)
SSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 core@${IP}"

while :; do
  now=$(date +%s); elapsed=$(( now - start ))
  if (( elapsed > MAX_SECS )); then
    echo "TIMEOUT after ${elapsed}s. Last known state:" >&2
    ssh "$PVE_HOST" "qm status ${SPIKE_VMID}; qm config ${SPIKE_VMID} | grep -E '^(net0|scsi0|ide2)'" >&2 || true
    exit 1
  fi

  if STATUS=$($SSH 'rpm-ostree status --booted 2>/dev/null | sed -n "s/^● *//p"' 2>/dev/null); then
    echo "[${elapsed}s] booted: ${STATUS}"
    if [[ "$STATUS" == *ublue-os/ucore* ]]; then
      echo "OK: ${IP} is running uCore"
      $SSH 'grep ^PRETTY_NAME /etc/os-release; ip -4 -o addr show scope global | awk "{print \"  \" \$2, \$4}"'
      exit 0
    fi
    # still on stock FCOS: check the provisioning unit rather than waiting blindly
    $SSH 'systemctl is-active fleet-provision' 2>/dev/null | grep -q '^activating' \
      && echo "         fleet-provision still running (pulling image)"
  else
    echo "[${elapsed}s] no ssh yet"
  fi
  sleep "$INTERVAL"
done
