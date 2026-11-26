# Container images

Four server images, built by GitHub Actions (`.github/workflows/containers.yml`) and
pushed to **ghcr.io**. The uCore VMs only ever pull — they need no build toolchain, no
source checkout, and no JDK.

`images.json` is the single source of truth for versions, sha256 pins and the plugins
each image carries. The workflow reads it to build its matrix; edit it and push to bump
anything.

| image | version | artifact | why |
|---|---|---|---|
| `papermc` | `26.3-258-p1` | `paper-26.3-258.jar` | Bukkit/Paper plugin API |
| `neoforge` | `26.3.0.115` | installer, resolved at build | modded server |
| `velocity` | `4.3.1-45` | `velocity-4.3.1-45.jar` | proxy; Velocity 4 requires Java 25 |
| `fabric` | `26.3-0.20.1-p1` | installer-resolved | Fabric server + OPanel; loader, libraries and Mojang jar are generated at build |

**One base for all four:** `eclipse-temurin:25-jre-ubi10-minimal` (glibc, 136 MB
compressed), kept current by Adoptium. `fabric` additionally uses the
`25-jdk-ubi10-minimal` variant in its build stage, because the Fabric installer is a
JDK app; nothing from that stage ships.

Why one base, and why this one:

- **glibc, not musl, because of `neoforge`.** It pulls netty's native epoll
  (`linux-x86_64`, glibc-built) and mods can ship glibc natives — and a base shared by
  all four therefore cannot be Alpine.
- **Temurin, not a hand-rolled Debian.** Adoptium publishes `alpine`, `jammy`, `noble`,
  `resolute` and `ubi10-minimal` — there is no Debian family. A `debian:slim` base would
  mean installing the JRE ourselves from a tarball or a second apt repo, i.e. a second
  set of pins for a runtime Adoptium already publishes.
- **No Ubuntu, by policy**, which leaves UBI 10 minimal as the glibc family.
- The three pure-JVM images (papermc, velocity, fabric) are on UBI for the sake of one
  shared base, not because they need glibc: they would run on Alpine at 78 MB instead of
  136 MB.
- UBI minimal specifics: it has no `util-linux`, so **`/usr/sbin/nologin` does not exist** —
  the service account uses `-s /bin/false`. It also has **no `tini`** (not in the UBI
  repos, no `catatonit` either), so reaping a server's child processes falls to the
  runtime's `--init`, which the fleet always passes.
- Base images are pinned by tag, not digest, so a rebuild can pick up a new base. Pin by
  `@sha256:` if that ever matters.

## Design rules these images follow

1. **Immutable runtime, mutable data.** Server binaries and loaders are baked in;
   worlds, configs, mods, plugins and logs live on the volume at `/data`, bind-mounted
   from a ZFS dataset. Swapping a server version is a new image tag, never an in-place edit.
2. **Every artifact is sha256-pinned** via `ADD --checksum`, where the publisher gives us
   one. A moved or tampered upstream asset fails the build instead of shipping. Paper and
   Velocity are self-verifying: their fill-data object id *is* the file's SHA-256. Plugin
   jars from `images.json` go through a `plugins` build stage instead — `curl` then
   jars from `images.json` go through a `plugins` build stage instead: `curl`, then
   `sha256sum -c` against the manifest's pin. Only `neoforge` and `fabric` are looser —
   their installer resolves a whole dependency tree at build time, so those pin versions
   only and a rebuild picks up whatever upstream currently serves for that version.
3. **No `latest` tag.** Every image is tagged with an exact version so a deploy can never
   change under a running server.
4. **Runs as uid/gid 1000**, non-root, so the bind-mounted dataset is the only thing it
   can write.
5. **Data dirs converge, and the volume always wins.** `lib/plugin-seed.sh` lands the
   image's plugin jars on the volume at every start, but never replaces a jar already
   there; it only retires a jar a previous fleet image put there that this one no longer
   bundles. Everything else on the volume is left alone.

## JVM flags

Deliberately minimal, per <https://exa.y2k.diy/garden/jvm-args/>:

```
-Xms<mem> -Xmx<mem> -XX:+UseZGC -XX:+UseCompactObjectHeaders
```

- `-Xms` always equals `-Xmx` (allocation/GC thrashing otherwise).
- ZGC + compact object headers is the whole recommendation for Java 25 / Minecraft 26.1+.
- Explicitly **not** used: `-XX:+AlwaysPreTouch` (only pays off above ~128 GiB),
  `-XX:+DisableExplicitGC` (breaks vanilla's out-of-memory trapdoor),
  `-XX:+ParallelRefProcEnabled` (default since Java 11), any Aikar-style flag set, and
  `-XX:+UnlockExperimentalVMOptions` (nothing here needs it).
- `-XX:+ExitOnOutOfMemoryError` is the one addition, and it is operational rather than a
  tuning flag: an OOM'd server must die so the unit restarts instead of hanging.

Tune the heap with `JAVA_MEMORY` (per image default: papermc/fabric `2G`, neoforge `4G`,
velocity `512M`–`1G`). Extra flags go in `JAVA_OPTS`.

## Running one

```sh
podman run -d --name lobby --init \
  --memory 4g --stop-timeout 60 \
  -p 10.10.10.54:25565:25565 \
  -v /srv/lobby/data:/data:Z \
  -e JAVA_MEMORY=2G \
  ghcr.io/<owner>/pumpkin-fleet/papermc:26.3-258-p1
```

Notes:

- `--init` is what reaps the server's child processes: the images have no `tini` (UBI has
  no such package).
- `--stop-timeout 60` gives the server time to save and shut down cleanly on `SIGTERM`.
- `EULA=true` is required for `papermc`, `neoforge` and `fabric`; without it the container
  has no `eula.txt` and the server refuses to start. `velocity` needs no EULA.
- `SERVER_PORT` only drives the healthcheck; the real port comes from each server's config.
- `fabric` bundles OPanel **and** its mandatory Fabric API dependency, seeding both into
  `/data/mods` on first start; without fabric-api the loader refuses to boot
  (`Mod 'OPanel' requires any version of fabric-api`). OPanel serves its panel on **3000**
  (host networking, no mapping) and its config is `/data/config/opanel.json`. Its first-run
  access key is written to `/opt/fabric/opanel/INITIAL_ACCESS_KEY.txt` — inside the
  container, not the volume — so read it with `podman exec <name> cat ...` before
  recreating the container, which regenerates it.
- `velocity` listens on `24587` by design (see `velocity.toml`), not the upstream default.

## Bumping a server

1. Edit the version and sha256 in `images.json` (fabric: the MC/loader/API/OPanel
   versions — it carries no sha256). A changed `plugins` list means a new version
   revision: `26.3-258` -> `26.3-258-p1`, because a version tag is never re-pushed.
2. Open a PR. The workflow builds all four and pushes only on `main`, so a PR proves it builds.
3. Merge, then point `configs/pins.env` at the new tag and deploy.

Nothing resolves a moving tag, so a running fleet is unaffected until the pins change.
