# Fabric + OPanel container image

Date: 2026-11-25
Status: draft for approval
Scope: `containers/fabric/` + an `images.json` entry. Originally image-only; on
2026-11-25 the user asked for it provisioned too, so the `vanilla` node (203,
`10.10.10.56`) now runs this image and `tasks/gameserver.py` grew the two fixes
that deploy exposed (see "Left to the builder" → resolved below).

## Intent

A fifth server image in the existing family: a Fabric server with OPanel, built
by CI and pushed to ghcr.io, so a Fabric node later needs nothing but a `podman
pull`. Nothing about the fleet's runtime changes yet.

## Goals

- Same shape as `neoforge`/`papermc`: pinned versions, resolved at build, no
  `latest`, runs as uid 1000, mutable data on `/data`, repo JVM flags.
- **No artifact URLs or binary uploads committed.** Versions are the only
  inputs; every jar is fetched at build time from the publisher.
- OPanel present in the image, seeded into the data volume on first start.

## Non-goals

- No VMID/IP allocation, no Velocity route, no authenticity/OIDC wiring.
- No `WORKLOADS` row in `tasks/gameserver.py`.
- No Fabric API / performance-mod set — an empty `mods/` besides OPanel.

## Anti-goals

- A signed/expiring URL in the repo (the `cos.ap-shanghai.myqcloud.com` links
  carry `q-signature` + `q-sign-time` and die in minutes).
- A jar uploaded into the repo, or a build that needs a network at *runtime*.
- A second JVM/GC flag scheme.

## Constraints (all verified 2026-11-25)

- **There is no downloadable self-contained Fabric server jar.** `FabricMC/
  fabric-loader` GitHub releases are tags with **zero assets**; maven ships only
  the loader library; `meta.fabricmc.net/v2/versions/loader/<mc>/<loader>/server`
  returns 404. The self-contained server is *generated* by `fabric-installer`
  (`-downloadMinecraft`): verified locally → `fabric-server-launch.jar` (616 B)
  + `libraries/` + `server.jar` (62 MB), for MC 26.3 / loader 0.20.1.
- **The launcher resolves `server.jar` and `libraries/` from the cwd**, not from
  its own location (verified: running it from `/tmp` fails with "The Minecraft
  server .JAR is missing"). So the process must run with the install dir as cwd —
  exactly the neoforge constraint, so it gets the neoforge treatment (symlink
  writable paths onto `/data`).
- **OPanel for Fabric is a plain jar** in `mods/`, not a native `.so`:
  `opanel-fabric-26.1-build-2.2.11.jar` from the GitHub release (61.4 MB,
  sha256 `d6cf3652986faafb5d277c22682da77c83a175c119973ece235ded8298032dd3`).
  The name means **minimum** MC version — 26.1 covers 26.3.
- OPanel needs Fabric Loader ≥ 0.17.3 for MC ≥ 1.21.11; 0.20.1 is the current
  stable loader and is what the pin uses.
- Fabric needs `eula.txt` (it is vanilla underneath), so `EULA=true` like paper.
- Base: **all five images share `eclipse-temurin:25-jre-ubi10-minimal`** (user
  decision 2026-11-25: Adoptium Temurin, no Ubuntu, one base). glibc is forced by
  pumpkin, so a shared base cannot be Alpine. `fabric` uses the `25-jdk` variant
  in its build stage only. Measured packages (amd64): Alpine 44, Temurin
  UBI-minimal 155, Temurin noble 108, Debian+Liberica 119 — UBI wins on nothing
  but the policy constraints above, and that is the trade accepted.
- **OPanel hard-depends on Fabric API** (found by running the image): without it
  the loader aborts with `Mod 'OPanel' requires any version of fabric-api`. The
  image bundles `fabric-api-0.167.0+26.3.jar` (1.8 MB) alongside OPanel and seeds
  both into `/data/mods`.
- Verified locally 2026-11-25 by building **and running** the image on both
  arm64 and amd64: server up in 1.8 s, `OPanel web server is ready on
  0.0.0.0:3000`, world/config/logs on the volume, mods seeded. Two build bugs
  found that way: the installer refuses a `-dir` that does not exist, and
  `/opt/fabric` must be chowned to 1000 or the entrypoint cannot symlink.

## Approaches

1. **Installer at build (chosen).** Build stage runs `fabric-installer` with the
   pinned MC + loader, `-downloadMinecraft`, into `/opt/fabric`. Mirrors
   `neoforge` exactly — that image already resolves its library set at build with
   the installer and ships no pins beyond the version. No URLs, no checksums, no
   binaries in the repo; a rebuild always fetches what upstream currently serves
   for that exact version pair.
2. **Commit the resolved jar set.** Reproducible offline, but it is precisely
   what was rejected: ~62 MB of binaries in git, and every loader bump becomes a
   binary commit.
3. **Resolve at container start.** No build-time network and no repo blobs, but
   the runtime then needs egress on first boot, breaks "immutable runtime,
   mutable data", and makes first-start latency unpredictable. Rejected.

**Chosen: 1.** It is the only one that satisfies both "download as needed" and
the existing image rules.

## Design

Namespace: `ghcr.io/jcpowermac/pumpkin-fleet/fabric:<version>`, tag
`<MC>-<loader>` (e.g. `26.3-0.20.1`) — matches the `<game>-<pin>` style of the
other images without a separate version field to keep in sync.

`containers/fabric/Dockerfile`:

- Stage `install`, `eclipse-temurin:25-jdk-ubi10-minimal` (the installer is a
  JDK app; it is a build stage only). Fetch
  `maven.fabricmc.net/.../fabric-installer-1.1.2.jar` — **the installer version
  is the one thing that stays literal in the Dockerfile**, fetched via a
  `ARG`, because it is tooling, not an artifact:

  ```
  ARG FABRIC_MC
  ARG FABRIC_LOADER
  ARG FABRIC_INSTALLER=1.1.2
  RUN java -jar fabric-installer-${FABRIC_INSTALLER}.jar server \
        -mcversion ${FABRIC_MC} -loader ${FABRIC_LOADER} \
        -downloadMinecraft -dir /opt/fabric
  ```

- Runtime stage `eclipse-temurin:25-jre-alpine`, `COPY --from=install
  /opt/fabric /opt/fabric`, `COPY entrypoint.sh`, uid/gid 1000, `WORKDIR /data`,
  `EXPOSE 25565`, same 180 s-start-period `/dev/tcp` healthcheck as neoforge.

`containers/fabric/entrypoint.sh` — neoforge's, minus the modded bits:

- `cd /opt/fabric`; symlink `world mods config logs crash-reports` onto `/data/…`
  (directories created there first, `rm -rf` the image copy, `ln -s`).
- `server.properties eula.txt ops.json whitelist.json banned-*.json
  usercache.json`: copy-to-volume-if-absent then symlink; `EULA=true` writes
  `eula.txt`.
- Seed OPanel: if `/data/mods/opanel-fabric-*.jar` is absent, copy the one from
  `/opt/opanel/` into the volume (rule 5: the volume always wins). Done here
  rather than in the image's `mods/` because `mods/` is volume state.
- `exec java -Xms/-Xmx ${JAVA_MEMORY:-2G} -XX:+UseZGC
  -XX:+UseCompactObjectHeaders -XX:+ExitOnOutOfMemoryError ${JAVA_OPTS:-}
  -jar fabric-server-launch.jar nogui`.

OPanel placement: the build stage downloads the release asset from
`github.com/opanel-mc/opanel/releases/download/2.2.11/opanel-fabric-26.1-build-2.2.11.jar`
into `/opt/opanel/` (61 MB, one layer). Trade-off, stated plainly: this deviates
from the pumpkin precedent, which deliberately keeps OPanel on the volume to
avoid 111 MB per layer. 61 MB is tolerable and keeps the image self-contained
("including OPanel" = the pull is sufficient). Revisit if the layer hurts.

`containers/images.json` entry — versions only, no sha256 (the installer and the
GitHub release are self-contained and re-resolve at build):

```json
{ "name": "fabric", "version": "26.3-0.20.1",
  "args": { "FABRIC_MC": "26.3", "FABRIC_LOADER": "0.20.1" },
  "opanel": { "version": "2.2.11", "asset": "opanel-fabric-26.1-build-2.2.11.jar" } }
```

The workflow needs **no change** — the matrix is `images.json`-driven. (Its
`args` step emits `KEY=VALUE`; the `opanel` object is ignored, as it already is
for pumpkin.) `containers/README.md` gets a row in both tables and a note on the
build-time resolution; `containers/README.md`'s "Bumping a server" gains nothing
new — editing the two versions is the whole bump.

## Left to the builder

- Whether the alpine base actually launches 26.3 (expect yes; if the loader
  trips on musl, switch the runtime base to `25-jre-ubi10-minimal` — one line).
- The exact OPanel data path: the pumpkin build reads
  `/data/plugins/libopanel_*.so`; the Fabric build almost certainly reads
  `/data/mods/*.jar`, and its panel port (3000) / config file name need one
  confirmation against a live start. Not needed for image-only scope.
- Whether to also wire `opanel` into `tasks/gameserver.py` for this image — out
  of scope now, and the existing pumpkin branch already shows the shape.
