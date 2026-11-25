# syntax=docker/dockerfile:1
# Velocity proxy on Eclipse Temurin 25 / UBI 10 minimal (glibc) — one base for
# the whole family (see papermc/Dockerfile for why glibc). Velocity 4 needs Java 25.
FROM eclipse-temurin:25-jre-ubi10-minimal

ARG VELOCITY_VERSION
ARG VELOCITY_SHA256

ADD --chmod=0644 --checksum=sha256:${VELOCITY_SHA256} \
    https://fill-data.papermc.io/v1/objects/${VELOCITY_SHA256}/velocity-${VELOCITY_VERSION}.jar \
    /opt/velocity.jar

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
ENV SERVER_PORT=24587
EXPOSE 24587
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD timeout 3 bash -c "</dev/tcp/127.0.0.1/${SERVER_PORT}" || exit 1
ENTRYPOINT ["/opt/entrypoint.sh"]
