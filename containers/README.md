# Container images

Five server images, built by GitHub Actions (`.github/workflows/containers.yml`) and
pushed to **ghcr.io**. The uCore VMs only ever pull — they need no build toolchain, no
source checkout, and no JDK.

`images.json` is the single source of truth for versions and sha256 pins. The workflow
reads it to build its matrix; edit it and push to bump anything.

| image | version | artifact | why |
|---|---|---|---|
| `pumpkin` | `0.2.2-26.3-26.60` | `pumpkin-X64-Linux` + `libpatchbukkit-X64-Linux.so` | Rust server, no JVM for the server itself; PatchBukkit needs a JRE 25 for plugin jars |
| `papermc` | `26.3-258` | `paper-26.3-258.jar` | Bukkit/Paper plugin API |
| `neoforge` | `26.3.0.115` | installer, resolved at build | modded server |
| `velocity` | `4.3.1-45` | `velocity-4.3.1-45.jar` | proxy; Velocity 4 requires Java 25 |
| `fabric` | `26.3-0.20.1` | installer-resolved | Fabric server + OPanel; loader, libraries and Mojang jar are generated at build |

**One base for all five:** `eclipse-temurin:25-jre-ubi10-minimal` (glibc, 136 MB
compressed), kept current by Adoptium. `fabric` additionally uses the
`25-jdk-ubi10-minimal` variant in its build stage, because the Fabric installer is a
JDK app; nothing from that stage ships.

Why one base, and why this one:

- **glibc is forced by `pumpkin`.** The Rust binary's interpreter is
  `/lib64/ld-linux-x86-64.so.2`, so it cannot run on musl — and a base shared by all
  five therefore cannot be Alpine. `neoforge` needs glibc too (netty's native epoll,
  and mods can ship glibc natives).
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
  repos, no `catatonit` either), so `pumpkin` relies on the runtime's `--init`.
- Base images are pinned by tag, not digest, so a rebuild can pick up a new base. Pin by
  `@sha256:` if that ever matters.

## Design rules these images follow

1. **Immutable runtime, mutable data.** Server binaries and loaders are baked in;
   worlds, configs, mods, plugins and logs live on the volume at `/data`, bind-mounted
   from a ZFS dataset. Swapping a server version is a new image tag, never an in-place edit.
2. **Every artifact is sha256-pinned** via `ADD --checksum`, where the publisher gives us
   one. A moved or tampered upstream asset fails the build instead of shipping. Paper and
   Velocity are self-verifying: their fill-data object id *is* the file's SHA-256. The two
   exceptions are `neoforge` and `fabric`, which resolve a whole dependency tree from an
   installer at build time — those pin only versions, so a rebuild picks up whatever
   upstream currently serves for that exact version.
3. **No `latest` tag.** Every image is tagged with an exact version so a deploy can never
   change under a running server.
4. **Runs as uid/gid 1000**, non-root, so the bind-mounted dataset is the only thing it
   can write.
5. **Data dirs are seeded once, never overwritten.** If a file already exists on the
   volume it is left alone — the volume always wins.

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

Tune the heap with `JAVA_MEMORY` (per image default: pumpkin/papermc `2G`, neoforge `4G`,
velocity `512M`–`1G`). Extra flags go in `JAVA_OPTS`.

## Running one

```sh
podman run -d --name lobby --init \
  --memory 4g --stop-timeout 60 \
  -p 10.10.10.54:25565:25565 \
  -v /srv/lobby/data:/data:Z \
  -e JAVA_MEMORY=2G \
  ghcr.io/<owner>/pumpkin-fleet/pumpkin:0.2.2-26.3-26.60
```

Notes:

- `--init` is **required** for `pumpkin`: PatchBukkit forks a child JVM and the image has
  no `tini` (UBI has no such package), so without `--init` children are never reaped.
- `--stop-timeout 60` gives the server time to save and shut down cleanly on `SIGTERM`.
- `EULA=true` is required for `papermc`, `neoforge` and `fabric`; without it the container
  has no `eula.txt` and the server refuses to start. `pumpkin` and `velocity` need no EULA.
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
   versions — it carries no sha256).
2. Open a PR. The workflow builds all five and pushes only on `main`, so a PR proves it builds.
3. Merge, then point `configs/pins.env` at the new tag and deploy.

Nothing resolves a moving tag, so a running fleet is unaffected until the pins change.
