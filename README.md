# pumpkin-fleet

Deterministic provisioning for the Minecraft fleet on pve3.

- **Plan:** `docs/plan.md` (copy of the deployment plan; authority lives in
  `~/Development/minecraft-server/docs/superpowers/plans/2026-11-21-pumpkin-fleet-deploy.md`)
- **Spec:** `~/Development/minecraft-server/docs/superpowers/specs/2026-11-21-pumpkin-fleet-lxc-pyinfra-design.html`
- **Spike notes / hard-won knowledge:** `docs/spike-notes.md` ← **read this before touching uCore**

## Current state

Game nodes are uCore (FCOS-based, immutable) KVM VMs. Infra nodes are LXC.

| VMID | Name | IP | What |
|---|---|---|---|
| 190 | `ucore-golden` | — | PVE template. **Reference image — do not clone it** (see spike notes §5) |
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
| `configs/pins.env` | Every pinned value in the fleet (IPs, VMIDs, image tags, PVE host). Single source; scripts `source` it. |
| `containers/` | `images.json` (server versions + sha256 pins + the plugins each image carries) and one Dockerfile per image: `papermc`, `fabric`, `velocity`, `neoforge`. Built in CI. |
| `.github/workflows/` | `containers.yml` — builds all four images and pushes versioned tags to ghcr.io. |
| `templates/fleet.ign.butane` | Per-node Ignition (envsubst → butane → ignition). Sets hostname + static IP, rebases to uCore, reboots, then enables Cockpit + QGA. |
| `scripts/fleet/` | Build the game nodes. `20-create-nodes.sh` is the real one. |
| `scripts/spike/` | The Task 1 spike, end to end. `30-create-vm.sh` builds one golden VM; `50-verify.sh` is non-destructive. |
| `inventory/`, `tasks/` | pyinfra: OS-level configuration (Task 2+). |
| `build/` | Gitignored working dir (downloads, rendered ignition, generated pve3 scripts). |
| `docs/` | Plan, `spike-notes.md`, and this README's references. |

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
