#!/bin/sh
set -eu
cd /opt/fabric

# Same split as neoforge: the launcher, libraries and Mojang jar are image
# content; world, mods, configs and logs are data.
#
# Fabric's launcher resolves server.jar and libraries/ relative to the cwd (it
# fails with "The Minecraft server .JAR is missing" anywhere else), so the
# process must run here — hence symlinking the writable paths onto /data.

# Directories: create on the volume, then link.
for p in world mods config logs crash-reports; do
  mkdir -p "/data/$p"
  rm -rf "/opt/fabric/$p"
  ln -s "/data/$p" "/opt/fabric/$p"
done

# Converge /data/mods onto the mods baked into the image: canonical names land, a mod
# the volume already holds is never replaced, and one this image no longer bundles is
# removed only if a previous fleet image put it there.
. /opt/lib/plugin-seed.sh
seed_plugins /opt/bundled-mods /data/mods fleet-mods

# Files: symlink only. A dangling link is correct — the server reads ENOENT as
# "absent" and creates the file on first write. Do NOT pre-create these: an empty
# ops.json/banned-*.json is a parse error, and eula.txt must be a file.
for p in server.properties eula.txt ops.json whitelist.json \
         banned-players.json banned-ips.json usercache.json; do
  if [ ! -e "/data/$p" ] && [ -e "/opt/fabric/$p" ] && [ ! -L "/opt/fabric/$p" ]; then
    cp -a "/opt/fabric/$p" "/data/$p"
  fi
  rm -f "/opt/fabric/$p"
  ln -s "/data/$p" "/opt/fabric/$p"
done

if [ "${EULA:-false}" = "true" ]; then echo "eula=true" > /data/eula.txt; fi

# Flags match the rest of the fleet (exa.y2k.diy/garden/jvm-args).
exec java "-Xms${JAVA_MEMORY:-2G}" "-Xmx${JAVA_MEMORY:-2G}" \
  -XX:+UseZGC -XX:+UseCompactObjectHeaders -XX:+ExitOnOutOfMemoryError \
  ${JAVA_OPTS:-} -jar fabric-server-launch.jar nogui
