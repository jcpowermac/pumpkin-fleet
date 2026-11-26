#!/usr/bin/env bash
# containers/lib/plugin-seed.sh is the converge rule the image entrypoints run on
# every start. The cases below are the ones deployed volumes depend on: canonical
# names land, the image's build of a name it ships replaces a stale one on the volume,
# an old-named copy of a jar the fleet ships is not left beside the canonical one, a jar
# the image stopped bundling is removed only if the fleet put it there, and any other
# name on the volume is the operator's.

set -u

REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SEED=$REPO/containers/lib/plugin-seed.sh
if [ ! -r "$SEED" ]; then
  echo "FAIL: $SEED is not readable"
  exit 1
fi
. "$SEED"

fail() { echo "FAIL: $1"; exit 1; }
present() { [ -e "$1" ] || fail "$2"; }
absent() { [ ! -e "$1" ] || fail "$2"; }
same() { cmp -s "$1" "$2" || fail "$3"; }

work=$(mktemp -d) || exit 1
trap 'rm -rf "$work"' EXIT

bundled=$work/image/plugins
data=$work/data/plugins
stamp=$data/.fleet-plugins
mkdir -p "$bundled" "$data"

# The baked set, named exactly as containers/images.json says.
printf 'luckperms 5.5.71\n' > "$bundled/LuckPerms.jar"
printf 'viaversion 5.12.3\n' > "$bundled/ViaVersion.jar"

# The volume as an older image left it: the same plugin under its upstream filename,
# a plugin the fleet shipped and no longer ships, a jar the fleet never shipped, and
# the previous build of a plugin whose canonical name the image still ships.
printf 'luckperms 5.5.71\n' > "$data/LuckPerms-Bukkit-5.5.71.jar"
printf 'vault 1.7.3\n' > "$data/Vault.jar"
printf 'someone elses plugin\n' > "$data/Extra.jar"
printf 'viaversion 5.12.2 stale\n' > "$data/ViaVersion.jar"
printf 'Vault.jar\n' > "$stamp"

# The entrypoints run `set -eu`, so the converge runs under it here too.
( set -eu; seed_plugins "$bundled" "$data" fleet-plugins )

present "$data/LuckPerms.jar" "first_seed_copies_all_canonical_and_writes_stamp: LuckPerms.jar never copied"
same "$bundled/LuckPerms.jar" "$data/LuckPerms.jar" "first_seed_copies_all_canonical_and_writes_stamp: copied bytes differ"
same "$bundled/ViaVersion.jar" "$data/ViaVersion.jar" "image_wins_for_a_bundled_name: the volume kept the stale ViaVersion.jar the image no longer ships"
present "$data/Extra.jar" "operator_owns_it: a jar the fleet never shipped was touched"
absent "$data/Vault.jar" "removal: Vault.jar was stamped but is no longer bundled, and was left behind"
absent "$data/LuckPerms-Bukkit-5.5.71.jar" "dedupe: old-named identical jar survived beside the canonical one"
present "$data/LuckPerms.jar" "dedupe: canonical LuckPerms.jar was removed instead of the duplicate"

printf 'LuckPerms.jar\nViaVersion.jar\n' > "$work/expected-stamp"
same "$work/expected-stamp" "$stamp" "stamp_after: stamp does not match the sorted bundled set"

# The entrypoint converges on every start, so a second pass must be a no-op.
( set -eu; seed_plugins "$bundled" "$data" fleet-plugins )
present "$data/LuckPerms.jar" "idempotent: a second converge removed a plugin it just installed"
present "$data/Extra.jar" "idempotent: a second converge touched a jar the fleet never shipped"
same "$work/expected-stamp" "$stamp" "idempotent: the stamp changed on a second converge"
same "$bundled/ViaVersion.jar" "$data/ViaVersion.jar" "idempotent: a second converge replaced a jar it had just installed"

# A plugins stage that produced nothing is not the same statement as "this fleet ships
# nothing": retiring the stamped set would strip a live volume in one start, and the
# emptied stamp would hide those names from the next good image.
empty_bundled=$work/empty-bundled
mkdir -p "$empty_bundled"
( set -eu; seed_plugins "$empty_bundled" "$data" fleet-plugins )
present "$data/LuckPerms.jar" "empty_bundle: an empty bundled dir retired a plugin the volume had"
present "$data/ViaVersion.jar" "empty_bundle: an empty bundled dir retired a plugin this image ships"
present "$data/Extra.jar" "empty_bundle: an empty bundled dir touched a jar the fleet never shipped"
same "$work/expected-stamp" "$stamp" "empty_bundle: the stamp was rewritten empty, losing the record to restore from"

echo "OK: seed_plugins converges a data dir onto the baked plugin set"
