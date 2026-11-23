#!/usr/bin/env bash
# 20-build-ignition.sh — render build/ignition.ign from templates/fleet.ign.butane.
# Runs on the laptop. Requires: butane (brew install butane).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
set -a; source configs/pins.env; set +a   # export pins for envsubst child

command -v butane >/dev/null || { echo "butane not found (brew install butane)" >&2; exit 1; }
[[ -s "${CORE_SSH_PUB_KEY_FILE}" ]] || { echo "missing ${CORE_SSH_PUB_KEY_FILE}" >&2; exit 1; }

CORE_SSH_PUB_KEY="$(cat "$CORE_SSH_PUB_KEY_FILE")" \
envsubst '${CORE_SSH_PUB_KEY} ${SPIKE_HOSTNAME} ${SPIKE_IP4} ${FLEET_GW} ${UCORE_IMAGE} ${UCORE_TAG}' \
  < templates/fleet.ign.butane > build/rendered.butane

# butane validates + compiles in one step.
butane -o build/ignition.ign build/rendered.butane
python3 -m json.tool build/ignition.ign >/dev/null   # must be valid JSON
echo "OK: build/ignition.ign ($(wc -c < build/ignition.ign | tr -d ' ') bytes)"
