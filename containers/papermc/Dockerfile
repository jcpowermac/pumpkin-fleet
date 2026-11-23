# syntax=docker/dockerfile:1
# Paper server on Eclipse Temurin 25.
FROM eclipse-temurin:25-jre-alpine

ARG PAPER_VERSION
# Paper's fill-data object id IS the file's SHA-256, so the URL is self-verifying.
ARG PAPER_SHA256

# bash exists only for the HEALTHCHECK's /dev/tcp probe; busybox sh has no /dev/tcp.
RUN apk add --no-cache bash ca-certificates

ADD --chmod=0644 --checksum=sha256:${PAPER_SHA256} \
    https://fill-data.papermc.io/v1/objects/${PAPER_SHA256}/paper-${PAPER_VERSION}.jar \
    /opt/paper.jar

COPY entrypoint.sh /opt/entrypoint.sh
# Alpine has no uid 1000 to reclaim; the data volume is owned by 1000, so match it.
RUN chmod 0755 /opt/entrypoint.sh \
 && addgroup -g 1000 minecraft \
 && adduser -u 1000 -G minecraft -D -h /data -s /sbin/nologin minecraft \
 && chown 1000:1000 /data

USER 1000:1000
WORKDIR /data
ENV SERVER_PORT=25565
EXPOSE 25565
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s \
  CMD timeout 3 bash -c "</dev/tcp/127.0.0.1/${SERVER_PORT}" || exit 1
ENTRYPOINT ["/opt/entrypoint.sh"]
