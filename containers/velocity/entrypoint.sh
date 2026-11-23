#!/bin/sh
set -eu
# Same GC rationale as the game images (exa.y2k.diy/garden/jvm-args): ZGC for
# low pause times. A proxy holds no world data, so the heap stays small.
exec java "-Xms${JAVA_MEMORY:-512M}" "-Xmx${JAVA_MEMORY:-1G}" \
  -XX:+UseZGC -XX:+UseCompactObjectHeaders -XX:+ExitOnOutOfMemoryError \
  ${JAVA_OPTS:-} -jar /opt/velocity.jar
