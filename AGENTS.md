# pumpkin-fleet

Pyinfra repo standing up the Minecraft fleet on **pve3** (172.31.12.99): 4 uCore
(FCOS/OSTree) game VMs (201-204), authentik LXC (150), plus existing Velocity/Traefik
LXCs. Access: `ssh root@172.31.12.99` (key-based); `pct exec <vmid>` for LXCs,
`qm` for VMs.

## Read first

- `docs/operations.md` — **the operator guide**: fleet table, infrastructure and state diagrams
  (Mermaid), day-2 runbooks, command reference, what the tooling refuses to do. Start here for
  anything that is a *how do I* question.
- `docs/superpowers/specs/2026-11-26-fleet-plugin-gitops-design.md` — the current fleet design
  (container images, `stable`/`stable-next` channels, the data zvol, the bump bot), with its plan
  in `docs/superpowers/plans/2026-11-26-fleet-plugin-gitops.md`. Newer than `docs/plan.md` where
  they disagree.
- `docs/plan.md` — the deployment plan (checkbox-tracked, one task at a time).
- `docs/spike-notes.md` — **everything learned the hard way**, organized as
  symptom-recognisable sections. Check it before fighting any infra problem.
- `docs/ldap-handoff.md` — authentik LDAP setup; current state in §10 (LDAP bind fix) and §11–12 (sssd on the game nodes + cockpit login, incl. all the sssd/cockpit gotchas).

## Scope note (2026-11-24)

**Ignore the legacy minecraft LXC fleet on pve3 (VMIDs 100-107: sr, velocity, usc,
CMCS, pangolin, cmcs2, traefik, lobby) — they are the old LXD/LXC stack being
replaced by this repo's uCore VMs (201-204) + LXC 150.** **Do not touch them** (2026-11-26): no migration, no copies
out of them, no stopping them — plan Task 8 is descoped. LXC 150 (authentik) is part of *this* fleet and stays in scope.

**Minecraft data lives on a second disk** (2026-11-26): each game VM has `scsi0` = disposable OS disk and `scsi1` =
host-owned ZFS volume `pool/mcdata/<node>`, attached as `/dev/zvol/...` so `qm destroy --purge` cannot delete it. Verified
on pve3 with a throwaway VMID. See the fleet design doc §2b.

## Scope note (2026-11-26): pumpkin is out

**Pumpkin (the Rust server + PatchBukkit + the native OPanel cdylib) is no longer used in
the fleet and is no longer in the tree.** `containers/images.json` holds the four server
images (`papermc`, `fabric`, `velocity`, `neoforge`) and `tasks/gameserver.py` `WORKLOADS`
maps `lobby -> papermc`. glibc is still required, but by `neoforge` (see
`containers/README.md`).

## Gotchas that have already cost time

- **Grep as `core` fails silently** (`Permission denied` → empty output reads as
  "line absent"). Use `sudo grep` on `/etc/ssh/**`, `/etc/sssd/**`, systemd units.
- **authentik LDAP bind flow = the provider's `authorization_flow`**, not
  `authentication_flow` (see spike-notes §15).
- authentik API calls need `Accept: application/json` or you get DRF's HTML page;
  flow endpoints live under `/api/v3/flows/instances/`.
- Nested `ssh … 'pct exec …'` quoting mangles — use
  `pct exec <id> -- bash -s <<'CT' … CT` and `pct push` for files.
- `rpm-ostree rebase` no-ops on an unchanged ref; rebase by `@sha256:<digest>` to pick
  up a rebuild of the same tag.
