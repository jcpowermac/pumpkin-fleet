# syntax=docker/dockerfile:1
# Paper server on Eclipse Temurin 25 / UBI 10 minimal (glibc) — one base for the
# whole family. glibc, not Alpine: neoforge pulls glibc-built natives (netty's
# native epoll, and mods can ship their own), and one base has to cover all four.

# The plugins declared in containers/images.json, fetched in a throwaway stage so the
# runtime stage stays a plain copy and its layer cache is not tied to the CDN.
# CI passes them as one flat list of `url install_as sha256` triples.
FROM docker.io/library/fedora:42 AS plugins
ARG PLUGIN_TRIPLETS
# An image carrying this stage declares plugins, so an empty arg means the manifest
# and the build disagree — fail rather than ship a server with none of them.
RUN set -eu; \
    if [ -z "${PLUGIN_TRIPLETS:-}" ]; then \
      echo "PLUGIN_TRIPLETS is empty but this image declares plugins" >&2; exit 1; \
    fi; \
    mkdir -p /opt/plugins; cd /opt/plugins; \
    set -- ${PLUGIN_TRIPLETS}; \
    while [ "$#" -ge 3 ]; do \
      curl -fsSL "$1" -o "$2"; echo "$3  $2" | sha256sum -c; shift 3; \
    done; \
    [ "$#" -eq 0 ] || { echo "PLUGIN_TRIPLETS is not a list of triples" >&2; exit 1; }
# shortcut: the arg is word-split, so no url or install_as may contain whitespace
# (all four CDNs satisfy); upgrade to a TSV build arg if one ever needs it.

FROM eclipse-temurin:25-jre-ubi10-minimal

ARG PAPER_VERSION
# Paper's fill-data object id IS the file's SHA-256, so the URL is self-verifying.
ARG PAPER_SHA256

ADD --chmod=0644 --checksum=sha256:${PAPER_SHA256} \
    https://fill-data.papermc.io/v1/objects/${PAPER_SHA256}/paper-${PAPER_VERSION}.jar \
    /opt/paper.jar

COPY --from=plugins /opt/plugins /opt/plugins
COPY containers/lib/plugin-seed.sh /opt/lib/plugin-seed.sh
COPY containers/papermc/entrypoint.sh /opt/entrypoint.sh
# UBI minimal ships no uid 1000, so it can be claimed directly, and no
# /usr/sbin/nologin either — hence /bin/false.
RUN chmod 0755 /opt/entrypoint.sh \
 && groupadd -g 1000 minecraft \
 && useradd -u 1000 -g 1000 -M -d /data -s /bin/false minecraft \
 && mkdir -p /data \
 && chown 1000:1000 /data \
 && chown -R 1000:1000 /opt/plugins /opt/lib

USER 1000:1000
WORKDIR /data
ENV SERVER_PORT=25565
EXPOSE 25565
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s \
  CMD timeout 3 bash -c "</dev/tcp/127.0.0.1/${SERVER_PORT}" || exit 1
ENTRYPOINT ["/opt/entrypoint.sh"]
