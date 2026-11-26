# Plugin + GitOps design for the pumpkin fleet

Date: 2026-11-26. Decided by jcallen; written by the assistant after research and a throwaway resolver spike.

## Intent and why

The fleet pins everything (OS image, server jars, sha256 of every artifact) but has no answer for
**third-party plugins** — the part of Minecraft hosting that actually hurts. It also has no answer for
"what happens after a PR merges": today a human runs pyinfra from a laptop, and the OS updates itself
on its own schedule. This design gives the fleet a plugin pipeline and a GitOps deploy path that fit
the stack it already has (uCore VMs + rootless podman + GitHub Actions + ghcr), and states the rebuild
flow that applies it end to end.

Chosen by jcallen: the **hybrid** shape (nodes pull, git gates content), the channel tag name
**`stable`**, and **automatic OS updates**. Levers are **Ignition** (first boot only) and **pyinfra**
(everything applied to a running node). Existing VMs 201–204 are the test environment for this design.

## Goals

- A declared, version-pinned, sha256-verified plugin/mod set per server image, resolvable from upstream
  without human hunting.
- A scheduled resolver that opens bump PRs; a human merge is the only way content changes.
- Merge → image built → **node converges by itself**; no laptop in the loop, no drift invisible to git.
- OS updates apply automatically, on a schedule, with a rollback path.
- Whole fleet reproducible from the repo: destroy 201–204, run the scripts, get a working fleet.

## Non-goals

- No Kubernetes, Argo CD, Flux, Helm. No FetchIt (pyinfra already delivers what it would).
- No auto-merged plugin bumps, no plugin hot-swap, no plugin UI/panel, no CurseForge automation.
- No per-node plugin divergence in v1: the plugin set belongs to the image, not the node.

## Anti-goals

- A plugin that "upgraded" into a version incompatible with the pinned server and took a node down.
- A silently knock-off dependency (search-discovered project instead of the canonical one).
- Converging four servers at once with no way back.
- A pipeline whose provenance is a moving tag with no record of what actually ran.

## Constraints (all verified on 2026-11-26, not assumed)

| Fact | Where |
|---|---|
| Nodes are created by re-importing the FCOS qcow2 with **per-node Ignition**; cloning the golden VM was tried and breaks boot | `scripts/fleet/20-create-nodes.sh` header |
| Ignition does: static IP → `rpm-ostree rebase` → reboot; rebase target today is `ghcr.io/ublue-os/ucore:stable-20261122` | `templates/fleet.ign.butane`, `configs/pins.env` |
| OS image is `ghcr.io/jcpowermac/pumpkin-os` built daily by BlueBuild (cron 06:00 UTC), **unsigned** (`--no-sign`), nodes use `ostree-unverified-registry` | `.github/workflows/os.yml`, `recipes/fleet.yml` |
| Nodes already run uCore's automatic OS updates: `AutomaticUpdatePolicy=stage`, `rpm-ostreed-automatic.timer` + `rpm-ostree-countme.timer` enabled. 201 is rebased to `pumpkin-os` (booted at `@sha256:f67bdb99…`, plus a staged `:latest` deployment `sha256:ef564d8a…`); **202/203/204 are still plain `ucore:stable-20261122`** | `rpm-ostree status` on 10.10.10.54–.57 |
| podman 6.1.3; quadlet generators present (`/usr/lib/systemd/system-generators/podman-system-generator`, `.../user-generators/podman-user-generator`) and **quadlet is not deprecated** — the deprecated command is `podman generate systemd`, whose man page says "We recommend using Quadlet files… no plans to remove"; podman 6.2.0 (2026-11-10) still adds Quadlet keys (`Umask`, `SetMountsDependency`, template instances) | podman man pages, podman release notes |
| Quadlet works rootless on a node: a `.container` drop-in produced `probe.service` via the user generator, `systemctl --user start` ran the container as `systemd-probe`; `AutoUpdate=registry` accepted in `[Container]`. Probe stopped and removed after the test | live test on 10.10.10.54 |
| `podman-auto-update.{service,timer}` ship at **both** system and user level; both disabled today. User level is the one that matches rootless quadlet. Timer is `OnCalendar=daily`, `RandomizedDelaySec=900`, `Persistent=true`, `ExecStartPost=podman image prune -f` | `/usr/lib/systemd/{system,user}/`, `systemctl cat` |
| Auto-update semantics: `io.containers.autoupdate` label or Quadlet `AutoUpdate` = `registry` → compares local digest to remote digest for **that tag**, pulls, and **restarts the systemd unit**. `--filter label=` / `--filter ancestor=IMAGE` scope it. `--dryrun` is not a flag in 6.1.3 | `podman-auto-update(1)` docs, `podman auto-update --help` |
| `containers/images.json` is the version/sha256 source of truth; `containers.yml` builds a matrix from it and pushes on merge, tagged by version, **no `latest`**, labelled `org.opencontainers.image.revision=<sha>` | `containers/images.json`, `.github/workflows/containers.yml` |
| `ADD --checksum` supports **sha256 only** | Dockerfile reference |
| Upstream hashes: Hangar publishes `sha256Hash` + a stable `hangarcdn.papermc.io` URL; Modrinth publishes sha1 + **sha512 only**; CurseForge is bot-hostile (Cloudflare challenge on www, 403 to a plain script probe, public Core API docs replaced by "CurseForge for Studios") | live API probes |
| Coverage is split: WorldEdit (`EngineTeam/WorldEdit`), ViaVersion, PlaceholderAPI on Hangar; LuckPerms, EssentialsX, Fabric API on Modrinth; **canonical Vault (`MilkBowl/Vault`, LGPL-3.0) on neither** — last GitHub release 1.7.3 (2020) | live API probes + GitHub API |
| Nodes already have git, python3, curl, jq, podman, `podman.socket` enabled by the deploy task | live probe, `tasks/gameserver.py` |
| No self-hosted GitHub runners exist (`actions/runners` → `[]`), so GitHub-hosted jobs cannot reach `10.10.10.0/24` | `gh api` |
| `scripts/mc-ping.py` returns version + `players` from a status ping — usable as a "server idle" gate | repo |
| Pumpkin is out of the fleet (2026-11-26 scope note); its image entry, the OPanel cdylib pin and `WORKLOADS lobby -> pumpkin` are stale | `AGENTS.md` |
| podman 6.1.3 `podman auto-update` accepts `--dry-run` and `--rollback` (**default true**, "Rollback to previous image if update fails"). Neither the CLI help nor `podman-auto-update(1)` defines "fails" as *the container went unhealthy*, so a server that starts fine with a broken plugin is **not** a rollback trigger | `podman auto-update --help` on 10.10.10.54 |
| `scripts/spike/10-download-fcos.sh` is tracked and already fetches + sha256-verifies the FCOS proxmoxve qcow2, but it resolves the release from the **live** streams JSON and then **rewrites `FCOS_BUILD`** in `pins.env` — today the pin is an output of the last run, not an input. Its generated PVE-side script lands in gitignored `build/` | `scripts/spike/10-download-fcos.sh` |
| `scripts/fleet/20-create-nodes.sh` depends on `/tmp/ucore-spike-fcos-$FCOS_BUILD.qcow2` already existing on pve3, on `/var/coreos/snippets/` existing, on a live read of the sssd bind credential from LXC 150, and on `butane` + the laptop's pubkey — and it resolves the FCOS release from the live streams JSON rather than from `FCOS_BUILD` | `scripts/fleet/20-create-nodes.sh`, `build/pve3-download.sh` |
| **`/etc` persists across an OSTree rebase.** On 201, which has been rebased uCore → `pumpkin-os`, everything pyinfra wrote is still there: firewalld `ports: 25565/tcp 3000/tcp`, `/etc/polkit-1/rules.d/60-cockpit-ops.rules`, `/etc/sudoers.d/cockpit-fleet`, `/etc/sssd/sssd.conf`. `/etc/firewalld/zones/` also holds a `FedoraServer.xml.old`, which is rpm-ostree's three-way /etc merge keeping a copy when the *base image* ships a different version of the same file — so a locally added drop-in persists, while state inside a file the image also owns can be rewritten. spike-notes §16.4's "does not survive a rebase" does not hold on this evidence | `firewall-cmd --list-all`, `ls /etc/polkit-1/rules.d /etc/sudoers.d /etc/firewalld/zones` on 10.10.10.54 |
| Only **201** (pumpkin, mc-ping answers, max 1000) and **203** (fabric, max 20) have a workload. **202 and 204 are empty VMs**: no container, no opened ports — the deploy task has never run on them | `podman ps -a` + `firewall-cmd --list-ports` on .55/.57, `scripts/mc-ping.py` from the laptop |
| A Minecraft client (and `scripts/mc-ping.py`) reaches a node **directly** on the LAN at `10.10.10.x:25565` — no Velocity needed | mc-ping from the laptop to .54 and .56 both answered |
| **A host-owned ZFS volume attached as a raw path survives a VM destroy.** Probe: `zfs create -V 1G pool/test-mcdata` → `qm create 299` → `qm set 299 --scsi1 /dev/zvol/pool/test-mcdata` (recorded as `scsi1: /dev/zvol/pool/test-mcdata,size=1G`) → `qm destroy 299 --purge` → the zvol was still there. PVE only purges volumes it owns (`pool:vm-<id>-disk-<n>`) | live probe on pve3, throwaway VMID 299, zvol removed afterwards |
| VM disks today are PVE-owned zvols: `scsi0: pool:vm-201-disk-0`, and `pool/vm-201-cloudinit`, `pool/vm-202-disk-0`, … — i.e. destroy-with-purge deletes them | `qm config 201`, `zfs list` |
| Spike result: a 9-plugin declared set resolved in 9 API calls / 4.8 s; EssentialsX correctly failed (max 26.1.2 vs Paper 26.3); resolver re-derived the existing `fabric-api 0.167.0+26.3` pin | `/tmp/bumpbot_spike.py` (throwaway) |

## Approaches considered

**A. CI-push.** Self-hosted runner on pve3 runs pyinfra on merge. *For:* smallest diff, one actor.
*Against:* inbound runner holding keys to every node; GitHub becomes the control plane; nothing
reconciles between merges; needs a new host.

**B. Node-pull, everything on the node.** Nodes reconcile units and images themselves. *For:* purest
GitOps. *Against:* reimplements config application (volume ownership in the podman namespace, linger,
firewalld) that pyinfra already does correctly, and adds bespoke code to four pets.

**C. Hybrid — chosen.** Git + CI gate content and publish a channel tag; **quadlet units +
`AutoUpdate=registry` + podman's own timer** converge the runtime on each node; **pyinfra** applies
everything that is genuinely imperative; **Ignition** stays first-boot only. Nothing new to install,
the fourth GitOps principle (continuous reconciliation) comes from podman, and rollback is a channel
move rather than a shell script. jcallen chose this and named the channel `stable`.

## The design

### 1. Tags: version tags stay, `stable` is the channel

- Every image keeps its exact version tag (`papermc:26.3-258`) — that is what gets built and audited.
- On a merge to `main` that changes an image's entry in `containers/images.json`, CI builds that version tag **and
  advances `<image>:stable` to the same digest**. `stable` is the only tag nodes reference, and it moves only by CI.
- **A version tag is never re-pushed.** A plugin change must therefore change the version, or nothing rebuilds and the
  version tag quietly becomes mutable — which is the thing rule 1 of `containers/README.md` exists to prevent.
  Plugin-bearing images carry a plugin revision in the version: `papermc:26.3-258-p1` → `26.3-258-p2` when only the
  plugin set changes, server part unchanged. CI enforces it: a PR that changes a `plugins` list without changing that
  image's `version` fails the build. My call; the alternative (let the version tag move) was rejected because §1's audit
  story depends on version tags being immutable.
- **Two channels, so a bad plugin set cannot reach four servers at once** (jcallen's decision: a canary node you can
  join with a Minecraft client). `stable` is what the fleet runs. `stable-next` is built from the same merge and is
  followed only by the canary node (202 `plugins`). A human joins the canary with a client, checks the set behaves, then
  promotes (`workflow_dispatch`) which moves `stable` to the same digest; the rest of the fleet converges on the next
  timer tick. `stable-next` is not a permanent extra maintenance burden — it is the same image content, retagged.
- Quadlet therefore says `Image=ghcr.io/jcpowermac/pumpkin-fleet/papermc:stable` + `AutoUpdate=registry`:
  podman compares the local digest with the digest `stable` now points at, pulls if different, restarts
  the unit. Convergence latency = the timer interval.
- `configs/pins.env` stops carrying a container tag to be edited by hand; the version source of truth is
  `images.json` and the channel is fixed (`stable`).
- **Rollback:** CI repoints `stable` at the previous digest (a `workflow_dispatch` job taking an explicit
  version from `images.json`). Nodes converge back on the next timer tick. No ssh needed.

Trade-off stated plainly: this fleet's "no moving tags" rule softens for one tag per image, and the
record of what moved and when is the ghcr tag history plus the commit that built the digest.

### 2. Node runtime: quadlet units, applied by pyinfra

Per node, for `core` (uid 1000), under `~/.config/containers/systemd/`:

```
# <node>.container  (from templates/quadlet/<image>.container + per-node drop-in)
[Unit]
Description=pumpkin fleet <node>
[Container]
Image=ghcr.io/jcpowermac/pumpkin-fleet/<image>:<channel>   # stable, or stable-next on the canary node
Volume=/srv/<node>/data:/data:Z
Environment=EULA=true
Environment=JAVA_MEMORY=<from WORKLOADS>
AutoUpdate=registry
[Service]
Restart=always
StopTimeoutSec=60
```

pyinfra (`tasks/gameserver.py` rewritten around this) owns: the data dir and its ownership
(`podman unshare chown -R 1000:1000`, host chown of the dir first — both already documented in the
current task), linger, `podman.socket`, firewalld ports, installing the unit + drop-in,
`systemctl --user daemon-reload`, `systemctl --user enable --now <node>.service`, and
`systemctl --user enable --now podman-auto-update.timer` (user level, because the quadlets are user
units). The current `_desired` pin-hash trick and `podman run` both go away: quadlet is the desired
state, podman is the reconciler. `--init` is provided by quadlet's generated unit (`Delegate=yes`);
verify the pumpkin PatchBukkit child-reaping concern is moot since pumpkin is out.

Config is one unit file per image plus a per-node drop-in, so four nodes never need four hand-written
units.

### 2b. The data disk: Minecraft data outlives the VM

(jcallen's decision, 2026-11-26: a second disk holds the persistent Minecraft server data.)

Each VM gets two disks:

- `scsi0` = OS, `pool:vm-<vmid>-disk-0`, imported from the pinned FCOS qcow2. **Disposable** — it is what a rebuild
  throws away, and the OS channel already updates it automatically (§4).
- `scsi1` = data, a ZFS volume the **host** owns, not PVE's VM storage: `zfs create -V <size> pool/mcdata/<node>`,
  attached as `qm set <vmid> --scsi1 /dev/zvol/pool/mcdata/<node>`. Verified above that `qm destroy --purge` does not
  delete it, which is exactly why it is created this way instead of as `pool:vm-<vmid>-disk-1`.

On the node: a systemd `.mount` unit mounted at `/srv/<node>/data` (the path `tasks/gameserver.py` already uses, so the
quadlet `Volume=` line does not change), formatted with XFS on first attach only — pyinfra formats when the device has
no filesystem (`blkid` empty) and never when it has one. Ownership keeps the existing dance: host `chown core:core` on
the mount point, then `podman unshare chown -R 1000:1000`, because uid 1000 inside the user namespace is not uid 1000 on
the host. SELinux: the quadlet keeps `:Z`; if a labelled relabel ever locks the server out of its own data, the escape
hatch already written in `tasks/gameserver.py` is `label=disable` + no `:Z`.

Sizes are a `pins.env` value per node group (start 40G; zpool `pool` has 1.9 TB at 6% used). A world's data is now a
host object with its own lifecycle: snapshots and backups (§ "not covered") operate on `pool/mcdata/<node>` without the
VM being involved.

### 3. Ignition: first boot only

`templates/fleet.ign.butane` keeps exactly what must exist before anything else can work: hostname,
static IP + gateway, `core`'s authorized key, sssd bind credential, and
`rpm-ostree rebase --bypass-driver ghcr.io/jcpowermac/pumpkin-os:stable` → reboot. Change of note: the
rebase target becomes the **fleet OS image on a moving channel**, not a pinned uCore tag, because OS
updates are automatic by decision. Nothing app-specific, no quadlet files, no plugin logic in Ignition.

`scripts/fleet/20-create-nodes.sh` stays the rebuild path: destroy VMID → re-import the pinned FCOS
qcow2 → per-node snippet → start. Add `pumpkin-os:stable` to `configs/pins.env` as the OS channel and
keep `FCOS_BUILD` pinned (the import source, not the running OS). Caveat found while planning it: the tracked
`scripts/spike/10-download-fcos.sh` already verifies the qcow2 hash, but it reads the live streams JSON and *overwrites*
`FCOS_BUILD` from what it finds, so the pin records the last run instead of controlling the next one.

### 4. OS updates: automatic, scheduled, reversible

- Keep uCore's default `AutomaticUpdatePolicy=stage` (a new deployment is prepared, activated by the
  next reboot). This is already what the nodes do.
- Add the missing half: a **reboot in a maintenance window**, so "automatic" is a schedule instead of
  "whenever a human or crash reboots". Per-node user timer, e.g. `OnCalendar=Sun *-*-* 05:00` with
  `RandomizedDelaySec=3600` so the four do not reboot together, gated by a service that:
  pings the node's server (`scripts/mc-ping.py` → `players.max`/`players.online`), skips (and logs
  "skipped, N players online") if anyone is connected, then `sudo systemctl reboot`.
- Rollback: rpm-ostree keeps the previous deployment; `rpm-ostree rollback` returns to it. Task 1 Step 7
  already proved rollback boots. Keep it as the documented escape hatch, and keep `rpm-ostree status` as
  the answer to "what is this node running".
- The OS channel is `pumpkin-os:stable`. BlueBuild's action only publishes `:latest`, so **`latest` is the canary's OS
  channel** (202 rebases to it) and `stable` advances only by an explicit `promote-os` `workflow_dispatch` once the canary
  boots clean and its server answers — the same two-channel shape as plugins, and the reason no production node follows a
  daily unreviewed build. OS *content* (packages, sssd) changes by PR; OS *currency* does not need one.

Unavoidable consequence, said out loud: `ostree-unverified-registry` + unsigned image + automatic update
means whoever can push to `ghcr.io/jcpowermac/pumpkin-os` can root four VMs. Mitigations in scope:
protected `main`, required review, and the bump bot never merging itself. The signing path is already
written down in `recipes/fleet.yml` and is a separate decision, not part of this design.

### 5. Plugin manifest and how jars get into the image

Plugins live **inside `containers/images.json`**, under each image's existing `plugins` key — the shape the pumpkin
OPanel entry already used. Not a second file: two places declaring the same jars is how you end up with two answers to
"what is loaded". Each entry declares the exact verified project, never a search term, because search picks knock-offs
(Vault is the proof):

```json
{ "name": "papermc", "version": "26.3-258-p1",
  "args": { "PAPER_VERSION": "26.3-258", "PAPER_SHA256": "c5df5fb9…" },
  "plugins": [ {
    "name": "LuckPerms", "slug": "luckperms", "source": "modrinth", "version": "5.5.71",
    "url": "https://cdn.modrinth.com/.../LuckPerms-Bukkit-5.5.71.jar",
    "target": "image", "install_as": "LuckPerms.jar",
    "sha256": "<computed once, committed>",
    "upstream_hashes": { "sha1": "fc8bb991…", "sha512": "188a91f0…" },
    "mc": ["26.3"], "deps": [], "note": "…" } ] }
```

- `source` ∈ `hangar | modrinth | github | manual`. Hangar gives sha256 natively; modrinth/github give
  sha1/sha512 or nothing, so the resolver computes the sha256 once and commits it. `manual` is the
  escape hatch for a canonical project with no script-friendly source (Vault, CurseForge/SpigotMC
  items): a human records the artifact's sha256 and the resolver only re-verifies it.
- `install_as` is load-bearing, not cosmetics. Upstream filenames carry versions (`LuckPerms-Bukkit-5.5.71.jar`), so a
  new bundle never name-matches the jar already sitting in `/data/plugins` and Paper ends up loading **both** copies.
  Every jar is renamed to a canonical name at build time, and the entrypoint records what the image ships in a stamp
  file `/data/plugins/.fleet-plugins` (one canonical name per line). Convergence is a stamp diff, not pattern matching, and
  **for a name the fleet ships, the image is authoritative**: a bundled canonical name absent from the data dir is
  copied; a bundled canonical name present with different bytes is replaced by the bundled build; a name the stamp
  recorded that the image no longer bundles is deleted; anything never in the stamp is untouched. Replacement is
  mandatory, not optional — canonical names are stable across releases, so "seed if absent, delete what is no longer
  bundled" would never upgrade anything, and a bump PR would change the image while every existing volume kept the old
  jar forever. To pin a custom build, ship it under a name the fleet does not use.
  This is the one deliberate exception to "the volume always wins", scoped to names the fleet itself shipped.
- **How they get into the image:** `ADD --checksum` cannot express a variable-length list and accepts sha256 only,
  which half the upstreams do not publish. So a `plugins` build stage takes the whole list as a single build arg (JSON)
  and loops `curl -fsSL "$url" -o "/opt/plugins/$jar"` then `echo "$sha256  $jar" | sha256sum -c`, failing the build on
  mismatch — the same guarantee `ADD --checksum` gives, with no Dockerfile templating and any list length. Server jars
  keep `ADD --checksum`; `containers/README.md` rule 2 is amended to say plugin jars are gated by an in-build sha256
  check instead.
- The entrypoint then seeds `/opt/plugins` (or `/opt/mods`) into `/data/plugins` | `/data/mods`, the way
  `containers/fabric/entrypoint.sh` already seeds bundled mods. `target` is `image` for everything in v1; the volume
  path (how OPanel's cdylib was installed) stays documented for a future native artifact, with no code written for it.
- The Fabric side uses the same manifest with `loaders: [fabric]` and `/opt/mods`; no second mechanism.

### 6. The bump bot

A scheduled job in this repo (GitHub Actions `schedule`, same runner class as the existing builds —
it needs no fleet access, only Hangar/Modrinth/GitHub):

1. Read `containers/plugins.json`.
2. Resolve each entry: Hangar `GET /api/v1/projects/<slug>/versions?platform=PAPER&platformVersion=<mc>&channel=Release&limit=N`
   (server-side compat filter, verified to work: WorldEdit → 7.4.6 for 26.3, 7.3.9 for 1.21.1, none for
   a fake version); Modrinth `/v2/project/<slug>/version` filtered by `loaders ∩ {paper,bukkit,spigot,folia}`
   or `{fabric}` **and** `game_versions ∋ <mc>` (this is what rejects the `26.4-pre-1` prerelease at the
   top of a version list); GitHub releases for `github`-source entries.
3. Expand dependency closure: Hangar `pluginDependencies[].projectId` resolved through
   `/api/v1/projects/<id>`; Modrinth `dependencies[].project_id`. Required deps become manifest entries
   automatically; optional deps are reported, not installed.
4. Download candidate jars, compute sha256, and write a branch + PR that edits `plugins.json` with:
   old → new version, upstream changelog URL, sha256, the compat evidence, declared deps added, and a
   **staleness warning** when the newest release predates the server pin (Vault 1.7.3 from 2020 is the
   example). Entries whose newest release does not declare the pinned MC version are reported as
   `incompatible: newest X supports …` and left alone. Entries with `source: manual` are only re-verified.
5. A human reviews and merges. Merge runs `containers.yml`, which builds the new version tag and
   advances `stable`. Convergence is then automatic (§1–2).

Rate limits are a non-issue at this scale: 9 declarations resolved in 9 calls / 4.8 s against Hangar's
20-per-5s and Modrinth's 300-per-minute. The job stays sequential with a small sleep.

### 7. Verification, because "port open" is not enough

After a bump lands, each node must prove the declared plugins loaded. GitHub cannot check this (no runner reaches
`10.10.10.0/24`), so it runs in two places: **on the node**, as a one-shot verify unit started after the container's
unit, and **from the laptop** (`tasks/verify.py`) whenever a human is looking. Add:

podman's own safety net is real but weaker than it sounds. `podman auto-update` has `--rollback` **default true**, yet
neither its help nor `podman-auto-update(1)` defines "update fails" as "the container went unhealthy", and
`containers/papermc/Dockerfile`'s HEALTHCHECK only tests port 25565 — a Paper server with an unmet plugin dependency
still opens the port. §7 is the plugin guard; podman rollback is not.

- `podman logs <node>` greps for each declared plugin's load line and for the failure shapes Paper
  emits (`NoClassDefFoundError`, `Unable to load`, `is not compatible`), failing loudly with the log tail.
- `scripts/mc-ping.py` proves the server answers and reports player count (also the reboot gate in §4).
- `rpm-ostree status` and `podman inspect --format '{{.Config.Image}}'` are the "what is actually running"
  answer, printed by the verify task.

Fabric gets this for free: the loader refuses to start on a missing/ incompatible mod and says why
("Mod 'OPanel' requires any version of fabric-api" is the fleet's own precedent). Paper does not, so
the log check is load-bearing there — upstream compat claims are author-supplied, and ViaVersion
declaring 1.16.5 *and* 26.3 in one build proves a filter cannot be trusted as proof.

### 7b. Promotion: how a new plugin set actually arrives

```
merge bump PR → CI builds papermc:26.3-258-p2 and papermc:stable-next → 
  202 (canary) converges within the timer → node verify unit reports: declared plugins loaded / failed
  → jcallen joins 10.10.10.55:25565 with a client and plays it
  → workflow_dispatch promote → papermc:stable = same digest
  → 201/203/204 converge on their next tick
  reject → promote never runs; `stable` never moved; revert the PR; canary rolls back by repointing stable-next
```

Direct LAN access to a node's port 25565 is already how mc-ping reaches 201 and 203, so the client check needs no new
network path — 202 just needs its container and its firewalld port, which is what the deploy task provides.

### 8. Rebuild end to end (the redeploy)

1. `configs/pins.env`: OS channel `pumpkin-os:stable`, FCOS import build pinned, node VMIDs/IPs as today.
2. `scripts/fleet/20-create-nodes.sh` → ensure `pool/mcdata/<node>` exists (§2b), destroy the VM **with its OS disk
   only** (`scsi0` detached first, or destroyed by name so the host-owned `scsi1` zvol is left alone), re-import the
   pinned qcow2 for `scsi0`, re-attach `/dev/zvol/pool/mcdata/<node>` as `scsi1`, then start; each node rebases to
   `pumpkin-os:stable` on first boot (~5–10 min, already the proven path).
3. `uv run pyinfra inventory/hosts.py --group game_nodes tasks/gameserver.py` → quadlet units, dirs and
   ownership, socket, firewalld, timers, enable+start.
4. Verify: unit active, image digest matches `stable`, declared plugins loaded, mc-ping answers.
5. OS updates then happen by themselves (§4); plugin updates only by merged bump PR (§6).

**Prerequisites §8 must own, not assume** (raised by the builder check, checked against the repo): the FCOS fetch is
already tracked (`scripts/spike/10-download-fcos.sh`) and already sha256-verifies the proxmoxve qcow2, but it must be
changed to fetch *the pinned* release, with `FCOS_BUILD=auto` as the only live-resolution case; `/var/coreos/snippets/`
must be created by a tracked step instead of surviving from the spike; the sssd bind credential read from LXC 150 must
fail loudly when 150 is down (150 is this fleet's own LXC, so reading it is not a legacy-LXC touch); and
`scripts/fleet/20-create-nodes.sh` must attach the data disk (§2b) on every create. Today a rebuild depends on spike-era
state that the repo does not create.

**Data is not at risk in a rebuild, by construction.** `docs/plan.md` Task 8 (copy worlds off the legacy LXCs) is
**descoped** — jcallen's instruction is to leave LXCs 100–107 alone, not read them, not copy from them, not stop them.
(The authentik LXC 150 is part of *this* fleet and stays in scope; its sssd bind read in `20-create-nodes.sh` is not a
legacy-LXC touch.) Because `/data` lives on a host-owned zvol (§2b), destroying and recreating a VM is a routine
operation that does not touch a world, so the automatic reboot timer (§4) and the rebuild (§8) are safe whether or not a
world is on the node. `fleet-reboot.timer` therefore needs no backup precondition; backups are what protect a world from
*itself*, and remain Task 9's job.

## Left to the builder

- Quadlet template layout: one unit file per image with a per-node drop-in is the stated shape; naming and where those
  drop-ins live in the repo is the builder's. `Network=host` and `Memory=` stay in the quadlet (spike-notes §16.3).
- Where the bump job lives (a new workflow file vs a job in `containers.yml`) and whether `stable` is advanced by the
  build job itself or by a separate dispatch.
- Whether the maintenance-window reboot is a node timer (stated here) or a pve3-side `qm reboot` scheduler; the node
  version keeps it inside the reconciled system.
- NeoForge mods are deferred to a later round: CurseForge is bot-hostile and automating it is an explicit non-goal.

Settled after the builder check, so a plan does not have to guess them: plugins inside `images.json` rather than a
second file; version tags never re-pushed, with a `-pN` plugin revision; a plugin build stage with an in-build sha256
gate instead of `ADD --checksum`; canonical jar names plus the `/data/plugins/.fleet-plugins` stamp as the removal
mechanism; verification split node-side + laptop; a tracked FCOS fetch script with the release pinned in `pins.env`; and
the backup gate on the reboot timer.

## What this design does not cover

Velocity (205) plugin handling, OPanel's replacement, backups of `/data` (except the gate in §8), Traefik/authentik
changes, image signing, and multi-node plugin divergence.

## Open, and only jcallen can answer

Ranked; each changes what gets built, not how the document reads.

1. ~~Canary posture~~ — **answered**: canary node 202 on `stable-next`, checked by joining it with a Minecraft client,
   `workflow_dispatch` promotion to `stable`. Written into §1 and §7b.
2. ~~Who owns node config~~ — **answered, and jcallen was right**: `/etc` persists across rebases, and the drop-ins
   pyinfra wrote on 201 survived its uCore → `pumpkin-os` rebase (see constraints). No OS-image baking needed; pyinfra
   stays the applicator and does not have to act as a reconciler. The one residual caveat: rpm-ostree's /etc merge can
   rewrite a file the base image also owns (`FedoraServer.xml.old`), so config that the image *ships* is the image's
   business and config the fleet *adds* is pyinfra's.
3. ~~What each node is for, in player terms~~ — **answered**: node roles are not a design input. Every VM is rebuildable
   and the names are labels, not purposes, so the design keys everything off `image + channel + data disk` per VMID and
   nothing depends on what a node is called or who plays on it. Node→image mapping stays a `pins.env`/`WORKLOADS` value
   the builder fills in, changeable at any time without touching the mechanism.
4. ~~Whether real worlds land on 201–204~~ — **answered**: leave the legacy LXCs alone (no migration), and put Minecraft
   data on the second disk (§2b) so it persists. Nothing in this design is a data-loss risk any more.

Nothing is left that only jcallen can answer. What follows are my calls, listed so they can be vetoed while reading:
plugins live inside `images.json`; version tags are never re-pushed and a plugin-only change bumps a `-pN` revision; a
`plugins` build stage gated by an in-build `sha256sum -c` instead of `ADD --checksum`; canonical jar names plus the
`/data/plugins/.fleet-plugins` stamp as the removal mechanism; two channels (`stable`, `stable-next`) with 202 as the
canary you join with a client and a `workflow_dispatch` promote; node-side verify units plus a laptop `tasks/verify.py`;
`AutomaticUpdatePolicy=stage` kept with a player-aware maintenance-window reboot timer; `pumpkin-os:stable` as the OS
channel in Ignition; host-owned `pool/mcdata/<node>` zvols as `scsi1`; a tracked FCOS fetch script with the release
pinned in `pins.env`.
