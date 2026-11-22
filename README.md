# pumpkin-fleet

Deterministic provisioning for the Minecraft fleet on pve3.

- **Plan:** `docs/plan.md` (copy of the deployment plan; authority lives in
  `~/Development/minecraft-server/docs/superpowers/`)
- **Spec:** `~/Development/minecraft-server/docs/superpowers/specs/2026-11-21-pumpkin-fleet-lxc-pyinfra-design.html`

## Layout

| Path | What |
|---|---|
| `configs/pins.env` | Every pinned value in the fleet (IPs, VMIDs, image tags, PVE host). Single source; scripts `source` it. |
| `templates/fleet.ign.butane` | FCOS/uCore ignition template (envsubst → butane → ignition). |
| `scripts/spike/` | Numbered, idempotent, `set -euo pipefail` scripts for the uCore VM spike (Task 1). Run in order, or `make spike`. |
| `inventory/`, `tasks/` | pyinfra: OS-level configuration (Task 2+). |
| `build/` | Gitignored working dir (downloads, rendered ignition, generated pve3 scripts). |
| `docs/` | Plan + spike/ops notes. |

## Conventions (determinism rules)

1. Every script: `set -euo pipefail`, sources `configs/pins.env`, no implicit state.
2. Re-runnable: re-running a script converges to the same result (checks before creating, destroys+recreates where PVE CLI lacks upserts).
3. Pinned over latest: image tags, FCOS build id, and script versions land in `configs/pins.env` / `docs/spike-notes.md` at run time.
4. PVE-side work is generated into `build/` and piped over SSH (`ssh root@… bash -s < build/…`) so the exact commands are reviewable artifacts.
5. Nothing touches pve3 without going through a script in this repo.
