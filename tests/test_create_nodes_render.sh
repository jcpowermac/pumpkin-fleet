#!/usr/bin/env bash
# scripts/fleet/20-create-nodes.sh --render-only has to be usable from a machine that cannot
# reach pve3 at all: the node rebuild is reviewed from the rendered Ignition before anything
# is pushed to a host. So the render path may not ssh, scp, qm or pct, and may not read the
# sssd bind credential out of CT 150.
#
# Proof shape: HOME is an empty directory holding a *public* key only (pins.env reads
# CORE_SSH_PUB_KEY_FILE=$HOME/.ssh/id_ed25519.pub), so no private key exists anywhere and ssh
# could not authenticate even if it were tried; ssh/scp/qm/pct are stubbed onto PATH to record
# any call. Nothing here contacts 172.31.12.99.

set -u

REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CREATE=$REPO/scripts/fleet/20-create-nodes.sh
if [ ! -r "$CREATE" ]; then
  echo "FAIL: $CREATE is not readable"
  exit 1
fi
command -v butane >/dev/null || { echo "SKIP: butane not installed (brew install butane)"; exit 0; }

fail() { echo "FAIL: $1"; exit 1; }

work=$(mktemp -d) || exit 1
trap 'rm -rf "$work"' EXIT

home=$work/home; mkdir -p "$home/.ssh"
printf 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOFFLINERENDERNOTAPRIVATEKEY fleet@test\n' > "$home/.ssh/id_ed25519.pub"
[ -e "$home/.ssh/id_ed25519" ] && fail "test HOME carries a private key"

bin=$work/bin; mkdir -p "$bin"
: > "$work/calls.log"
for c in ssh scp qm pct; do
  cat > "$bin/$c" <<EOF
#!/usr/bin/env bash
echo "$c \$*" >> "$work/calls.log"
exit 1
EOF
  chmod +x "$bin/$c"
done

out=$(HOME="$home" PATH="$bin:$PATH" bash "$CREATE" --render-only 2>&1); code=$?
[ "$code" -eq 0 ] || fail "--render-only exited $code: $out"
[ -s "$work/calls.log" ] && fail "--render-only contacted pve3: $(cat "$work/calls.log")"

# The channel split, per node: three follow the promoted tag, the canary node the canary tag.
lines=$(grep -h "rpm-ostree rebase" "$REPO"/build/nodes/*.butane)
[ "$(wc -l <<<"$lines")" -eq 4 ] || fail "expected four rebase lines, got: $lines"
for n in lobby plugins vanilla neoforge; do
  # The .ign is what pve3 is handed; a render that produced no ignition is not a render.
  [ -s "$REPO/build/nodes/$n.ign" ] || fail "$n produced no ignition (build/nodes/$n.ign is empty)"
done
for n in lobby vanilla neoforge; do
  grep -q 'ostree-unverified-registry:ghcr.io/jcpowermac/pumpkin-os:stable' \
    "$REPO/build/nodes/$n.butane" || fail "$n is not on the stable OS channel: $lines"
done
grep -q 'ostree-unverified-registry:ghcr.io/jcpowermac/pumpkin-os:latest' \
  "$REPO/build/nodes/plugins.butane" || fail "canary node plugins is not on the canary tag: $lines"

# The guard must key on the rendered image, not on a hardcoded uCore grep: a node booted on
# pumpkin-os would otherwise rebase + reboot forever (docs/spike-notes.md §14).
for n in lobby plugins; do
  grep -q 'rpm-ostree status --booted 2>/dev/null | grep -q "ghcr.io/jcpowermac/pumpkin-os"' \
    "$REPO/build/nodes/$n.butane" || fail "$n kept the hardcoded uCore guard"
done

# Known limit of this mode, asserted so it cannot be mistaken for a deployable config: the
# sssd bind fields are empty because reading them is a pve3 call.
grep -q 'ldap_default_bind_dn = cn=,ou=users' "$REPO/build/nodes/lobby.butane" \
  || fail "--render-only leaked a bind credential into the render"

# A typo in CANARY_NODE must not be a green run that quietly leaves every node on the promoted
# channel with nothing following the canary build. Run the same script against a pins copy whose
# CANARY_NODE matches no node name (separate tree: the script sources configs/pins.env by path).
alt=$work/alt; mkdir -p "$alt/scripts/fleet" "$alt/configs" "$alt/templates"
cp "$CREATE" "$alt/scripts/fleet/20-create-nodes.sh"
cp "$REPO/templates/fleet.ign.butane" "$alt/templates/"
sed 's/^CANARY_NODE=plugins$/CANARY_NODE=plugns/' "$REPO/configs/pins.env" > "$alt/configs/pins.env"
out2=$(HOME="$home" PATH="$bin:$PATH" bash "$alt/scripts/fleet/20-create-nodes.sh" --render-only 2>&1); code2=$?
[ "$code2" -ne 0 ] || fail "CANARY_NODE=plugns left all four nodes on the promoted channel with exit 0"
case "$out2" in *plugns*) ;; (*) fail "the CANARY_NODE failure did not name the value: $out2" ;; esac
[ -s "$work/calls.log" ] && fail "the typo run contacted pve3: $(cat "$work/calls.log")"

echo "OK: --render-only renders all four nodes offline, on the right channel, contacting nothing"
