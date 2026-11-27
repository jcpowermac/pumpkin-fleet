# pumpkin-fleet

Deterministic provisioning for the Minecraft fleet on pve3.

- **Operator guide:** `docs/operations.md` — fleet table, infrastructure + state diagrams, day-2
  runbooks (deploy, verify, bump a plugin, promote, roll back, migrate a world, rebuild a node),
  command reference. Start here for a *how do I* question.
- **Design spec:** `docs/superpowers/specs/2026-11-26-fleet-plugin-gitops-design.md` (images,
  channels, data zvols, bump bot) with its plan in
  `docs/superpowers/plans/2026-11-26-fleet-plugin-gitops.md`.
- **Plan (older, checkbox-tracked):** `docs/plan.md` — superseded by the spec above where they disagree.
- **Spike notes / hard-won knowledge:** `docs/spike-notes.md` ← **read this before touching uCore**

## Current state

Game nodes are uCore (FCOS-based, immutable) KVM VMs. Infra nodes are LXC.

| VMID | Name | IP | What |
|---|---|---|---|
| 190 | `ucore-golden` | — | PVE template. **Reference image — do not clone it** (see spike notes §5) |
| 150 | `authentik` (LXC) | 10.10.10.53 | identity: LDAP logins for node ssh and Cockpit |
| 201 | `lobby` | 10.10.10.54 | uCore, Cockpit :9090, QGA |
| 202 | `plugins` | 10.10.10.55 | uCore, Cockpit :9090, QGA |
| 203 | `vanilla` | 10.10.10.56 | uCore, Cockpit :9090, QGA |
| 204 | `neoforge` | 10.10.10.57 | uCore, Cockpit :9090, QGA |
| 205 | `velocity` | 10.10.10.52 | not built yet — Velocity runs here as a container |

The servers themselves run as containers on those VMs. Images are built by GitHub
Actions and pulled from ghcr.io — see `containers/README.md`.

## Layout

| Path | What |
|---|---|
| `docs/operations.md` | The operator guide: diagrams, runbooks, command reference. |
| `configs/pins.env` | Every pinned value in the fleet (IPs, VMIDs, image tags, channels, reboot window, data device, PVE host). Single source; scripts `source` it. |
| `containers/` | `images.json` (server versions + sha256 pins + the plugins each image carries) and one Dockerfile per image: `papermc`, `fabric`, `velocity`, `neoforge`. Built in CI. |
| `recipes/fleet.yml` | BlueBuild recipe for the node OS image (`ghcr.io/jcpowermac/pumpkin-os`: uCore + sssd + python3). |
| `.github/workflows/` | `containers.yml` builds/pushes the four images and is the only thing that moves a server channel; `os.yml` builds the OS image daily and is the only thing that moves `pumpkin-os:stable`; `bump-plugins.yml` opens the weekly plugin bump PR. |
| `scripts/fleet/` | The node lifecycle: `15-ensure-data-disks.sh` (world zvol + `scsi1`), `20-create-nodes.sh` (create/recreate the VMs), `25-migrate-data.sh` (move a world onto the zvol), `mount.py`, `10-make-template.sh`. |
| `scripts/` | `resolve_plugins.py` (plugin resolution + pins), `ci_gate.py` (what may be published), `promote.sh` (channel moves), `render_quadlet.py` (node units), `fleet/reboot-gate.py` + `fleet/verify-plugins.py` (run on the node), `mc-ping.py`. |
| `templates/fleet.ign.butane` | Per-node Ignition (envsubst → butane → ignition). Sets hostname + static IP, rebases to uCore, reboots, then enables Cockpit + QGA. |
| `scripts/spike/` | The Task 1 spike, end to end. `30-create-vm.sh` builds one golden VM; `50-verify.sh` is non-destructive. |
| `inventory/`, `tasks/` | pyinfra: OS-level configuration and the deploy/verify applicator (`tasks/gameserver.py`, `tasks/verify.py`). |
| `build/` | Gitignored working dir (downloads, rendered ignition, generated pve3 scripts). |
| `tests/` | The offline suite: resolver, gate, manifests, quadlet/mount/render, node scripts against a simulated pve3, workflow text rules. No docker, no network, no pve3. |
| `docs/` | Operator guide, plan, `spike-notes.md`, and the specs/plans this repo implements. |

## Conventions (determinism rules)

1. Every script: `set -euo pipefail`, sources `configs/pins.env`, no implicit state.
2. Re-runnable: re-running a script converges to the same result (checks before creating, destroys+recreates where PVE CLI lacks upserts).
3. Pinned over latest: image tags, FCOS build id, and script versions land in `configs/pins.env` / `docs/spike-notes.md` at run time. Exception: the Cockpit container is intentionally unpinned — uCore should pivot its own updates.
4. PVE-side work is generated into `build/` and piped over SSH (`ssh root@… bash -s < build/…`) so the exact commands are reviewable artifacts.
5. Nothing touches pve3 without going through a script in this repo.

## Rules that cost hours to learn

These are invariants, not preferences. Full detail in `docs/spike-notes.md`.

1. **Do not clone a booted FCOS/uCore template.** `coreos-gpt-setup` rewrites the root fs UUID
   mid-boot and Ignition's `useradd` fails into an emergency shell. Create nodes by
   `--scsi0 <storage>:0,import-from=<qcow2>` so each gets a real first boot.
2. **Ignition is one-shot per system.** Re-arming `ignition.firstboot` to replay it is a trap.
3. **Ignition config arrives via the cloud-init cidata drive** (`--ide2 <storage>:cloudinit` +
   `--cicustom vendor=<storage>:snippets/<file>`), not `fw_cfg`.
4. **SeaBIOS, not OVMF**, and `virtio-scsi-pci`, not `virtio-scsi-single`.
5. **Never use `curl -f` as an egress probe** — ghcr.io returns 405 on `/` and 401 on `/v2/`.
6. **`rpm-ostree rebase` needs `--bypass-driver`** on stock FCOS.
7. **A rebase-and-reboot unit needs a completion guard** or it reboot-loops forever.
8. **The deployment's `/etc` is the immutable image copy** — never edit a stopped disk's
   `/etc` and expect the guest to see it. Writable state lives under `/ostree/deploy/<os>/var`.
