#!/usr/bin/env bash
# 40-wait-rebase.sh — wait for the spike VM to boot and finish the two-step uCore rebase.
# Runs on the laptop. Polls ssh + rpm-ostree. Max ~35 min (rebase pulls ~2 GB from ghcr).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
source configs/pins.env

SSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 core@${IP_SPIKE}"
MAX_ROUNDS=140   # 140 * 15s = 35 min

echo "waiting for first SSH (FCOS + ignition, static IP ${IP_SPIKE}) ..."
for i in $(seq 1 "$MAX_ROUNDS"); do
  if $SSH 'true' 2>/dev/null; then echo "SSH up (round $i)"; break; fi
  sleep 15
  if [[ $i -eq $MAX_ROUNDS ]]; then echo "FAIL: no SSH after 35 min" >&2; exit 1; fi
done

echo "waiting for signed uCore deployment (${UCORE_IMAGE}:${UCORE_TAG}) ..."
for i in $(seq 1 "$MAX_ROUNDS"); do
  STATE="$($SSH 'test -f /etc/ucore-autorebase/signed && rpm-ostree status 2>/dev/null | head -8 || echo "rebooting..."' 2>/dev/null || echo unreachable)"
  if grep -q "ucore" <<<"$STATE" && $SSH 'test -f /etc/ucore-autorebase/signed' 2>/dev/null; then
    echo "uCore rebase complete (round $i):"
    $SSH 'rpm-ostree status | head -8; hostnamectl'
    exit 0
  fi
  sleep 15
  if [[ $i -eq $MAX_ROUNDS ]]; then echo "FAIL: no signed uCore deployment after 35 min; last state:" >&2; echo "$STATE" >&2; exit 1; fi
done
