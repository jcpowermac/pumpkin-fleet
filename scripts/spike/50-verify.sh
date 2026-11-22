#!/usr/bin/env bash
# 50-verify.sh — spike acceptance: identity, cockpit, guest agent, rebase/rollback cycle.
# Runs on the laptop. Exit 0 = PASS (spike notes updated by hand after).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
source configs/pins.env
SSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 core@${IP_SPIKE}"
PASS=0; FAIL=0
ok()   { echo "PASS: $1"; PASS=$((PASS+1)); }
bad()  { echo "FAIL: $1"; FAIL=$((FAIL+1)); }
wait_ssh() { for i in $(seq 1 60); do $SSH 'true' 2>/dev/null && return 0; sleep 5; done; return 1; }
booted_ref() { $SSH 'rpm-ostree status | sed -n 1p'; }

# 1. Identity: uCore deployment booted, signed marker present.
if $SSH 'test -f /etc/ucore-autorebase/signed && rpm-ostree status | sed -n 1p | grep -q ucore'; then
  ok "uCore deployment booted + signed marker"
else
  bad "uCore identity (rpm-ostree status / signed marker)"
fi
$SSH 'head -3 /etc/os-release'

# 2. Cockpit reachable on 9090.
CODE="$(curl -ks -o /dev/null -w '%{http_code}' --max-time 10 "https://${IP_SPIKE}:9090" || echo 000)"
if [[ "$CODE" =~ ^[23] ]]; then ok "cockpit ${IP_SPIKE}:9090 (HTTP $CODE)"; else bad "cockpit (HTTP $CODE)"; fi

# 3. Guest agent (informational — record actual state in spike notes).
echo "guest agent: $(ssh "$PVE_HOST" "qm gueststatus ${SPIKE_VMID}" 2>/dev/null | tr '\n' ' ')"

# 4. Rollback cycle: booted ref must flip between signed and unverified, and return.
BEFORE="$(booted_ref)"
echo "before rollback: $BEFORE"
$SSH 'sudo rpm-ostree rollback && sudo systemctl reboot' || true
wait_ssh || bad "SSH after rollback reboot"
sleep 20
MID="$(booted_ref)"
echo "after rollback:  $MID"
if [[ "$BEFORE" == *"image-signed"* && "$MID" == *"unverified"* ]]; then
  ok "rollback booted previous (unverified) deployment"
else
  bad "rollback deployment flip (before=$BEFORE mid=$MID)"
fi

echo "restoring latest deployment + reboot ..."
$SSH 'sudo rpm-ostree rollback && sudo systemctl reboot' || true
wait_ssh || bad "SSH after restore reboot"
sleep 20
AFTER="$(booted_ref)"
echo "after restore:   $AFTER"
if [[ "$AFTER" == "$BEFORE" ]]; then ok "back on signed deployment"; else bad "restore to signed deployment (after=$AFTER)"; fi

echo "----"
echo "spike verify: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
