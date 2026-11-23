# Container images

Four server images, built by GitHub Actions (`.github/workflows/containers.yml`) and
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

Base image for all four: `eclipse-temurin:25-jre-noble`.

## Design rules these images follow

1. **Immutable runtime, mutable data.** Server binaries and loaders are baked in;
   worlds, configs, mods, plugins and logs live on the volume at `/data`, bind-mounted
   from a ZFS dataset. Swapping a server version is a new image tag, never an in-place edit.
2. **Every artifact is sha256-pinned** via `ADD --checksum`. A moved or tampered upstream
   asset fails the build instead of shipping. Paper and Velocity are self-verifying: their
   fill-data object id *is* the file's SHA-256.
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

- `--init` matters for `pumpkin` (PatchBukkit forks a JVM child that must be reaped).
- `--stop-timeout 60` gives the server time to save and shut down cleanly on `SIGTERM`.
- `EULA=true` is required for `papermc` and `neoforge`; without it the container has no
  `eula.txt` and Paper refuses to start. `pumpkin` and `velocity` need no EULA.
- `SERVER_PORT` only drives the healthcheck; the real port comes from each server's config.
- `velocity` listens on `24587` by design (see `velocity.toml`), not the upstream default.

## Bumping a server

1. Edit the version and sha256 in `images.json`.
2. Open a PR. The workflow builds all four and pushes only on `main`, so a PR proves it builds.
3. Merge, then point `configs/pins.env` at the new tag and deploy.

Nothing resolves a moving tag, so a running fleet is unaffected until the pins change.
