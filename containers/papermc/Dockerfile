# syntax=docker/dockerfile:1
# Paper server on Eclipse Temurin 25 / UBI 10 minimal (glibc) — one base for the
# whole family. glibc, not Alpine: neoforge pulls glibc-built natives (netty's
# native epoll, and mods can ship their own), and one base has to cover all four.
FROM eclipse-temurin:25-jre-ubi10-minimal

ARG PAPER_VERSION
# Paper's fill-data object id IS the file's SHA-256, so the URL is self-verifying.
ARG PAPER_SHA256

ADD --chmod=0644 --checksum=sha256:${PAPER_SHA256} \
    https://fill-data.papermc.io/v1/objects/${PAPER_SHA256}/paper-${PAPER_VERSION}.jar \
    /opt/paper.jar

COPY entrypoint.sh /opt/entrypoint.sh
# UBI minimal ships no uid 1000, so it can be claimed directly, and no
# /usr/sbin/nologin either — hence /bin/false.
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
ENTRYPOINT ["/opt/entrypoint.sh"]
