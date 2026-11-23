#!/bin/sh
set -eu
cd /opt/neoforge

# Everything the server writes at runtime belongs on the volume. The loaders and args
# are image content; world, mods, configs and logs are data.
#
# NeoForge's generated unix_args.txt uses paths relative to this install root, so the
# process must run here — hence symlinking the writable paths out onto /data instead of
# running with /data as the working directory.

# Directories: create on the volume, then link.
for p in world mods config defaultconfigs logs crash-reports; do
  mkdir -p "/data/$p"
  rm -rf "/opt/neoforge/$p"
  ln -s "/data/$p" "/opt/neoforge/$p"
done

# Files: symlink only. A dangling link is correct — the server reads ENOENT as "absent"
# and creates the file on first write. Do NOT mkdir or pre-create these: an empty
# ops.json/banned-*.json is a parse error, and eula.txt must be a file, not a directory.
for p in server.properties eula.txt ops.json whitelist.json \
         banned-players.json banned-ips.json usercache.json; do
  if [ ! -e "/data/$p" ] && [ -e "/opt/neoforge/$p" ] && [ ! -L "/opt/neoforge/$p" ]; then
    cp -a "/opt/neoforge/$p" "/data/$p"
  fi
  rm -f "/opt/neoforge/$p"
  ln -s "/data/$p" "/opt/neoforge/$p"
done

if [ "${EULA:-false}" = "true" ]; then echo "eula=true" > /data/eula.txt; fi

# Flags: exa.y2k.diy/garden/jvm-args for Java 25 / Minecraft 26.1+ — ZGC + compact object
# headers, matched Xms/Xmx, nothing else. ExitOnOutOfMemoryError is operational (a hung
# OOM'd server must die so the unit restarts), not a tuning flag.
exec java "-Xms${JAVA_MEMORY:-4G}" "-Xmx${JAVA_MEMORY:-4G}" \
  -XX:+UseZGC -XX:+UseCompactObjectHeaders -XX:+ExitOnOutOfMemoryError \
  ${JAVA_OPTS:-} \
  @libraries/net/neoforged/neoforge/"${NEOFORGE_VERSION}"/unix_args.txt nogui
