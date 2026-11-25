# syntax=docker/dockerfile:1
# Pumpkin (Rust) + PatchBukkit (Bukkit plugin support) on Eclipse Temurin 25.
#
# Base is UBI 10 minimal (glibc). The Rust binary's interpreter is
# /lib64/ld-linux-x86-64.so.2, so musl/Alpine cannot run it at all, and Temurin
# publishes no Debian variant — UBI is the glibc base.
#
# Pumpkin needs no JVM itself; the JRE is only there because PatchBukkit spawns a
# Java 25 VM to run Bukkit/Spigot/Paper plugin jars.
FROM eclipse-temurin:25-jre-ubi10-minimal

ARG PUMPKIN_RELEASE
ARG PUMPKIN_SHA256
ARG PATCHBUKKIT_VERSION
ARG PATCHBUKKIT_SHA256

# Server binary. The release tag contains "+", which must be percent-encoded in the URL.
ADD --chmod=0755 --checksum=sha256:${PUMPKIN_SHA256} \
    https://github.com/Pumpkin-MC/Pumpkin/releases/download/${PUMPKIN_RELEASE}/pumpkin-X64-Linux \
    /opt/pumpkin

# PatchBukkit is a native plugin library loaded from the server's plugins/ directory.
# It is seeded from here into the volume on first start, so the image stays the pin.
# mkdir first: ADD from a URL creates implicit parents as 0644, which uid 1000
# cannot traverse, so the entrypoint's `[ -e ]` guard silently skipped the seed.
RUN mkdir -p -m 0755 /opt/bundled-plugins
ADD --chmod=0644 --checksum=sha256:${PATCHBUKKIT_SHA256} \
    https://github.com/Pumpkin-MC/PatchBukkit/releases/download/${PATCHBUKKIT_VERSION}/libpatchbukkit-X64-Linux.so \
    /opt/bundled-plugins/libpatchbukkit-X64-Linux.so

COPY entrypoint.sh /opt/entrypoint.sh
# UBI minimal ships no uid 1000, so it can be claimed directly.
RUN chmod 0755 /opt/entrypoint.sh \
 && groupadd -g 1000 minecraft \
 && useradd -u 1000 -g 1000 -M -d /data -s /bin/false minecraft \
 && mkdir -p /data \
 && chown 1000:1000 /data

USER 1000:1000
WORKDIR /data
ENV SERVER_PORT=25565
EXPOSE 25565
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s \
  CMD timeout 3 bash -c "</dev/tcp/127.0.0.1/${SERVER_PORT}" || exit 1
# No tini: UBI has no tini package. PatchBukkit forks a child JVM, so run this
# container with --init (podman/docker) or the child is never reaped.
ENTRYPOINT ["/opt/entrypoint.sh"]
