#!/bin/sh
set -eu
cd /opt/neoforge

# Everything the server writes at runtime belongs on the volume. The loaders/args are
# image content; world, mods, configs and logs are data.
for p in world mods config defaultconfigs logs crash-reports \
         server.properties eula.txt ops.json whitelist.json \
         banned-players.json banned-ips.json usercache.json; do
  if [ ! -e "/data/$p" ]; then
    if [ -e "/opt/neoforge/$p" ]; then cp -a "/opt/neoforge/$p" "/data/$p"
    else mkdir -p "/data/$p"; fi
  fi
  rm -rf "/opt/neoforge/$p"
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
