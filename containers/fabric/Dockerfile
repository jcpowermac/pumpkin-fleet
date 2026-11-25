# syntax=docker/dockerfile:1
# Fabric server (+ OPanel) on Eclipse Temurin 25 / UBI 10 minimal (glibc) — the
# same base as every other image in this directory. glibc, not Alpine: it is the
# only base all five images can share (the pumpkin binary cannot run on musl).
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


FROM eclipse-temurin:25-jre-ubi10-minimal

ARG FABRIC_MC
ARG FABRIC_LOADER
ARG FABRIC_API
ARG OPANEL_VERSION
ARG OPANEL_ASSET
ENV FABRIC_MC=${FABRIC_MC} \
    FABRIC_LOADER=${FABRIC_LOADER} \
    FABRIC_API=${FABRIC_API} \
    OPANEL_VERSION=${OPANEL_VERSION}

COPY --from=install /opt/fabric /opt/fabric
COPY entrypoint.sh /opt/entrypoint.sh

# Bundled mods, seeded into the volume on first start so the image stays the pin.
# mkdir first: ADD from a URL creates implicit parents as 0644, which uid 1000
# cannot traverse.
#
# OPanel is a plain mod jar here (not Pumpkin's native cdylib), and it hard-depends
# on Fabric API — without it the loader refuses to start with
# "Mod 'OPanel' requires any version of fabric-api".
RUN mkdir -p -m 0755 /opt/bundled-mods
ADD --chmod=0644 \
    https://github.com/opanel-mc/opanel/releases/download/${OPANEL_VERSION}/${OPANEL_ASSET} \
    /opt/bundled-mods/opanel-fabric.jar
ADD --chmod=0644 \
    https://github.com/FabricMC/fabric/releases/download/${FABRIC_API}/fabric-api-${FABRIC_API}.jar \
    /opt/bundled-mods/fabric-api.jar

# UBI minimal ships no uid 1000, so it can be claimed directly, and no
# /usr/sbin/nologin either — hence /bin/false.
RUN chmod 0755 /opt/entrypoint.sh \
 && groupadd -g 1000 minecraft \
 && useradd -u 1000 -g 1000 -M -d /data -s /bin/false minecraft \
 && mkdir -p /data \
 && chown 1000:1000 /data \
 && chown -R 1000:1000 /opt/fabric

USER 1000:1000
WORKDIR /data
ENV SERVER_PORT=25565
EXPOSE 25565
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s \
  CMD timeout 3 bash -c "</dev/tcp/127.0.0.1/${SERVER_PORT}" || exit 1
ENTRYPOINT ["/opt/entrypoint.sh"]
