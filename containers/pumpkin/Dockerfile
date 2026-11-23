# syntax=docker/dockerfile:1
# Pumpkin (Rust) + PatchBukkit (Bukkit plugin support) on Eclipse Temurin 25.
#
# Pumpkin is a native Rust binary and needs no JVM. The JRE is present only because
# PatchBukkit spawns a Java 25 VM to run Bukkit/Spigot/Paper plugin jars.
FROM eclipse-temurin:25-jre-noble

ARG PUMPKIN_RELEASE
ARG PUMPKIN_SHA256
ARG PATCHBUKKIT_VERSION
ARG PATCHBUKKIT_SHA256

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

# Server binary. The release tag contains "+", which must be percent-encoded in the URL.
ADD --chmod=0755 --checksum=sha256:${PUMPKIN_SHA256} \
    https://github.com/Pumpkin-MC/Pumpkin/releases/download/${PUMPKIN_RELEASE}/pumpkin-X64-Linux \
    /opt/pumpkin

# PatchBukkit is a native plugin library loaded from the server's plugins/ directory.
# It is seeded from here into the volume on first start, so the image stays the pin.
ADD --chmod=0644 --checksum=sha256:${PATCHBUKKIT_SHA256} \
    https://github.com/Pumpkin-MC/PatchBukkit/releases/download/${PATCHBUKKIT_VERSION}/libpatchbukkit-X64-Linux.so \
    /opt/bundled-plugins/libpatchbukkit-X64-Linux.so

COPY entrypoint.sh /opt/entrypoint.sh
# The temurin base already ships uid/gid 1000 (an "ubuntu" user). The data volume is
# owned by 1000, so reclaim the id rather than running as a different uid.
RUN chmod 0755 /opt/entrypoint.sh \
 && if getent passwd 1000 >/dev/null; then userdel -f "$(getent passwd 1000 | cut -d: -f1)" || true; fi \
 && if getent group 1000 >/dev/null; then groupdel "$(getent group 1000 | cut -d: -f1)" || true; fi \
 && groupadd -g 1000 minecraft \
 && useradd -u 1000 -g 1000 -M -d /data -s /usr/sbin/nologin minecraft \
 && mkdir -p /data \
 && chown 1000:1000 /data

USER 1000:1000
WORKDIR /data
ENV SERVER_PORT=25565
EXPOSE 25565
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s \
  CMD timeout 3 bash -c "</dev/tcp/127.0.0.1/${SERVER_PORT}" || exit 1
# tini because PatchBukkit forks a child JVM that must be reaped.
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/entrypoint.sh"]
