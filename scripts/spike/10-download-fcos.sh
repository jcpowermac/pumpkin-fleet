#!/usr/bin/env bash
# 10-download-fcos.sh — fetch FCOS stable (proxmoxve qcow2) onto pve3, sha256-verified.
# Stream metadata: raw.githubusercontent.com/coreos/fedora-coreos-streams (GitHub moved builds
# off builds.coreos.org to builds.coreos.fedoraproject.org — see ledger ruling, Task 1).
# Runs on the laptop; PVE-side work is generated into build/pve3-download.sh and piped over SSH.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
source configs/pins.env

STREAM_JSON_URL="https://raw.githubusercontent.com/coreos/fedora-coreos-streams/main/streams/stable.json"

cat > build/pve3-download.sh <<EOF
#!/usr/bin/env bash
set -euo pipefail
T=/tmp/fcos-stable
mkdir -p "\$T"
curl -fsSL --retry 3 -o "\$T/stable.json" "$STREAM_JSON_URL"
RELEASE=\$(python3 -c "import json;d=json.load(open('\$T/stable.json'));print(d['architectures']['x86_64']['artifacts']['proxmoxve']['release'])")
URL=\$(python3 -c "import json;d=json.load(open('\$T/stable.json'));print(d['architectures']['x86_64']['artifacts']['proxmoxve']['formats']['qcow2.xz']['disk']['location'])")
SHA=\$(python3 -c "import json;d=json.load(open('\$T/stable.json'));print(d['architectures']['x86_64']['artifacts']['proxmoxve']['formats']['qcow2.xz']['disk']['sha256'])")
echo "FCOS release: \$RELEASE"
OUT=/tmp/ucore-spike-fcos-\${RELEASE}.qcow2
if [[ -s "\$OUT" ]] && echo "\$OUT \$SHA" | sha256sum -c --quiet; then
  echo "reusing verified \$OUT"
else
  curl -fL --retry 3 -o "\$T/proxmoxve.qcow2.xz" "\$URL"
  echo "\$SHA  \$T/proxmoxve.qcow2.xz" | sha256sum -c
  xz -dk -c "\$T/proxmoxve.qcow2.xz" > "\$OUT"
fi
echo "FCOS_BUILD=\$RELEASE"
EOF

# Run on pve3; capture the resolved release back into pins.env.
OUT="$(ssh "$PVE_HOST" bash -s < build/pve3-download.sh)"
echo "$OUT"
RELEASE="$(grep -oE 'FCOS_BUILD=.+' <<<"$OUT" | cut -d= -f2)"
[[ -n "$RELEASE" ]] || { echo "no FCOS_BUILD in output" >&2; exit 1; }
sed -i.bak "s/^FCOS_BUILD=.*/FCOS_BUILD=$RELEASE/" configs/pins.env && rm -f configs/pins.env.bak
echo "OK: FCOS $RELEASE proxmoxve qcow2 on pve3 (/tmp/ucore-spike-fcos-$RELEASE.qcow2), pinned in configs/pins.env"
