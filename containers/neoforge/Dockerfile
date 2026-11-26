# syntax=docker/dockerfile:1
# NeoForge server on Eclipse Temurin 25 / UBI 10 minimal (glibc).
#
# glibc, not Alpine: NeoForge pulls netty's native epoll (linux-x86_64, glibc-built)
# and mods can ship glibc natives. Temurin has no Debian variant.
#
# The installer is run at build time so the runtime image needs no network and the
# resolved library set is frozen into the image. NeoForge's generated unix_args.txt
# uses paths relative to its install root, so the server must run with that as cwd;
# the writable paths are symlinked onto the volume by the entrypoint.
FROM eclipse-temurin:25-jre-ubi10-minimal AS install

ARG NEOFORGE_VERSION
ARG NEOFORGE_SHA256

ADD --checksum=sha256:${NEOFORGE_SHA256} \
    https://maven.neoforged.net/releases/net/neoforged/neoforge/${NEOFORGE_VERSION}/neoforge-${NEOFORGE_VERSION}-installer.jar \
    /tmp/installer.jar

RUN mkdir -p /opt/neoforge \
 && java -jar /tmp/installer.jar --installServer /opt/neoforge \
 && rm -f /tmp/installer.jar


FROM eclipse-temurin:25-jre-ubi10-minimal

ARG NEOFORGE_VERSION
ENV NEOFORGE_VERSION=${NEOFORGE_VERSION}

COPY --from=install /opt/neoforge /opt/neoforge
COPY neoforge/entrypoint.sh /opt/entrypoint.sh

# UBI minimal ships no uid 1000, so it can be claimed directly.
RUN chmod 0755 /opt/entrypoint.sh \
 && groupadd -g 1000 minecraft \
 && useradd -u 1000 -g 1000 -M -d /data -s /bin/false minecraft \
 && mkdir -p /data \
 && chown 1000:1000 /data \
 && chown -R 1000:1000 /opt/neoforge

USER 1000:1000
WORKDIR /data
ENV SERVER_PORT=25565
EXPOSE 25565
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s \
  CMD timeout 3 bash -c "</dev/tcp/127.0.0.1/${SERVER_PORT}" || exit 1
ENTRYPOINT ["/opt/entrypoint.sh"]
