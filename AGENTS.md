# pumpkin-fleet

Pyinfra repo standing up the Minecraft fleet on **pve3** (172.31.12.99): 4 uCore
(FCOS/OSTree) game VMs (201-204), authentik LXC (150), plus existing Velocity/Traefik
LXCs. Access: `ssh root@172.31.12.99` (key-based); `pct exec <vmid>` for LXCs,
`qm` for VMs.

## Read first

- `docs/plan.md` — the deployment plan (checkbox-tracked, one task at a time).
- `docs/spike-notes.md` — **everything learned the hard way**, organized as
  symptom-recognisable sections. Check it before fighting any infra problem.
- `docs/ldap-handoff.md` — authentik LDAP setup; current state in §10 (LDAP bind fix) and §11–12 (sssd on the game nodes + cockpit login, incl. all the sssd/cockpit gotchas).

## Scope note (2026-11-24)

**Ignore the legacy minecraft LXC fleet on pve3 (VMIDs 100-107: sr, velocity, usc,
CMCS, pangolin, cmcs2, traefik, lobby) — they are the old LXD/LXC stack being
replaced by this repo's uCore VMs (201-204) + LXC 150.**

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
