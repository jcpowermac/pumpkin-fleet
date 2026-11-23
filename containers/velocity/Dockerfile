# syntax=docker/dockerfile:1
# Velocity proxy on Eclipse Temurin 25. Velocity 4 requires Java 25.
FROM eclipse-temurin:25-jre-noble

ARG VELOCITY_VERSION
ARG VELOCITY_SHA256

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ADD --chmod=0644 --checksum=sha256:${VELOCITY_SHA256} \
    https://fill-data.papermc.io/v1/objects/${VELOCITY_SHA256}/velocity-${VELOCITY_VERSION}.jar \
    /opt/velocity.jar

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
ENV SERVER_PORT=24587
EXPOSE 24587
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD timeout 3 bash -c "</dev/tcp/127.0.0.1/${SERVER_PORT}" || exit 1
ENTRYPOINT ["/opt/entrypoint.sh"]
