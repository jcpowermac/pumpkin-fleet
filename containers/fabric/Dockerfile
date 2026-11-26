# syntax=docker/dockerfile:1
# Fabric server (+ OPanel) on Eclipse Temurin 25 / UBI 10 minimal (glibc) — the
# same base as every other image in this directory. glibc, not Alpine: neoforge
# pulls glibc-built natives (netty's native epoll, and mods can ship their own),
# and one base has to cover all four.
#
# There is no downloadable self-contained Fabric server jar: the loader's GitHub
# releases carry no assets, maven ships only the loader library, and Fabric's meta
# /server endpoint is gone. The installer *generates* one, so it runs here at build
# time (same shape as neoforge): the versions are the only inputs and every jar —
# loader, mixin, ASM, libraries, the Mojang server jar — is resolved fresh.
FROM eclipse-temurin:25-jdk-ubi10-minimal AS install

ARG FABRIC_MC
ARG FABRIC_LOADER
ARG FABRIC_INSTALLER=1.1.2

ADD https://maven.fabricmc.net/net/fabricmc/fabric-installer/${FABRIC_INSTALLER}/fabric-installer-${FABRIC_INSTALLER}.jar \
    /tmp/fabric-installer.jar

# -dir must already exist; the installer will not create it.
RUN mkdir -p /opt/fabric \
 && java -jar /tmp/fabric-installer.jar server \
      -mcversion "${FABRIC_MC}" -loader "${FABRIC_LOADER}" \
      -downloadMinecraft -dir /opt/fabric \
 && rm -f /tmp/fabric-installer.jar

# The mods declared in containers/images.json (Fabric API, OPanel, LuckPerms),
# fetched in a throwaway stage: Fabric's loader refuses to start when a mod it
# expects is missing, so these are as much build input as the loader itself.
# CI passes them as one flat list of `url install_as sha256` triples. OPanel is a
# plain mod here and hard-depends on Fabric API — without it the loader refuses to
# start with "Mod 'OPanel' requires any version of fabric-api".
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

ARG FABRIC_MC
ARG FABRIC_LOADER
ENV FABRIC_MC=${FABRIC_MC} \
    FABRIC_LOADER=${FABRIC_LOADER}

COPY --from=install /opt/fabric /opt/fabric
# /opt/plugins -> /opt/bundled-mods: the loader looks in mods/, and the entrypoint
# seeds from there into /data/mods.
COPY --from=plugins /opt/plugins /opt/bundled-mods
COPY lib/plugin-seed.sh /opt/lib/plugin-seed.sh
COPY fabric/entrypoint.sh /opt/entrypoint.sh

# UBI minimal ships no uid 1000, so it can be claimed directly, and no
# /usr/sbin/nologin either — hence /bin/false.
RUN chmod 0755 /opt/entrypoint.sh \
 && groupadd -g 1000 minecraft \
 && useradd -u 1000 -g 1000 -M -d /data -s /bin/false minecraft \
 && mkdir -p /data \
 && chown 1000:1000 /data \
 && chown -R 1000:1000 /opt/fabric /opt/bundled-mods /opt/lib

USER 1000:1000
WORKDIR /data
ENV SERVER_PORT=25565
EXPOSE 25565
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s \
  CMD timeout 3 bash -c "</dev/tcp/127.0.0.1/${SERVER_PORT}" || exit 1
ENTRYPOINT ["/opt/entrypoint.sh"]
