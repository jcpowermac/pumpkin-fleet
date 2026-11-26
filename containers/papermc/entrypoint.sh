#!/bin/sh
set -eu
# Converge /data/plugins onto the jars baked into the image before Paper scans it: for a
# plugin name this image ships, the image is authoritative; a jar under any other name is
# the operator's.
. /opt/lib/plugin-seed.sh
seed_plugins /opt/plugins /data/plugins fleet-plugins
if [ "${EULA:-false}" = "true" ]; then echo "eula=true" > /data/eula.txt; fi
# Flags follow exa.y2k.diy/garden/jvm-args for Java 25 / Minecraft 26.1+:
# ZGC (sub-ms pauses, scales with heap) and compact object headers. Nothing else.
# Xms and Xmx match to avoid allocation/GC thrashing. ExitOnOutOfMemoryError is an
# operational flag, not a tuning one: a hung OOM'd server must die so the unit restarts.
exec java "-Xms${JAVA_MEMORY:-2G}" "-Xmx${JAVA_MEMORY:-2G}" \
  -XX:+UseZGC -XX:+UseCompactObjectHeaders -XX:+ExitOnOutOfMemoryError \
  ${JAVA_OPTS:-} -jar /opt/paper.jar --nogui
