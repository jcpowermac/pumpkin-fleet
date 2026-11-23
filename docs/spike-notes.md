# uCore VM spike notes

Task 1 of `docs/plan.md`. **Status: PASS** (2026-11-22).

Everything below was learned the hard way; each item has a symptom so it is recognisable next time.

---

## Current state (as built)

| VMID | Name | IP | Notes |
|---|---|---|---|
| 190 | `ucore-golden` | — | PVE **template**. Reference image only — **do not clone it**, see §5 |
| 201 | `lobby` | 10.10.10.54 | uCore, Cockpit :9090, QGA |
| 202 | `plugins` | 10.10.10.55 | uCore, Cockpit :9090, QGA |
| 203 | `vanilla` | 10.10.10.56 | uCore, Cockpit :9090, QGA |
| 204 | `neoforge` | 10.10.10.57 | uCore, Cockpit :9090, QGA |

- Base image: FCOS `45.20261030.3.1`, artifact `proxmoxve` / `qcow2.xz`
  (sha256 `afb24063a5464ce6b7edd7ac84440b408e40699886fbff9f01d2b8582a9f2dba`).
- uCore target: `ostree-unverified-registry:ghcr.io/ublue-os/ucore:stable-20261122`
  (digest `sha256:b4f1969034401d4e95a0573c105ea745433db6357b8dfc33d5188f8389367706`).
- Shape: 4 cores / 4096 MB / 40 GiB on `pool`, virtio-net on `vmbr1`, `virtio-scsi-pci`, SeaBIOS.
- Rebase time: ~5–6 min per node with 4 pulls in parallel from ghcr.io.

**Open discrepancy:** the plan specifies **6144 MB for `neoforge`** (4 GiB heap + JVM overhead).
All four nodes were built at 4096 MB. `qm set 204 --memory 6144` + power cycle still to do.

---

## 1. The working recipe (authoritative)

Source: <https://docs.fedoraproject.org/en-US/fedora-coreos/provisioning-proxmoxve/>

```bash
# storage for snippets + images — `content` is the property name
mkdir -p /var/coreos/snippets /var/coreos/images
pvesm add dir fleet --path /var/coreos --content images,snippets

# ignition goes in the snippets dir
install -m0644 ignition.ign /var/coreos/snippets/<node>.ign

qm create <vmid> --name <node> --cores 4 --memory 4096 --cpu host \
  --net0 virtio,bridge=vmbr1 --scsihw virtio-scsi-pci \
  --agent enabled=1 --tablet 1 --vga serial0 --serial0 socket
qm set   <vmid> --scsi0 "pool:0,import-from=/var/coreos/images/fcos.qcow2"
qm resize <vmid> scsi0 40G
qm set   <vmid> --ide2 pool:cloudinit          # the cidata drive Ignition reads
qm set   <vmid> --boot order=scsi0
qm set   <vmid> --cicustom vendor=fleet:snippets/<node>.ign
qm set   <vmid> --ciupgrade 0
qm set   <vmid> --ipconfig0 ip=<ip>/24,gw=10.10.10.1
qm start <vmid>
```

Implementation: `scripts/fleet/20-create-nodes.sh` (nodes), `scripts/spike/30-create-vm.sh` (spike).

---

## 2. Ignition delivery (the big one)

**Symptom:** `Ignition: no config provided by user`, then
`Ignition: timed out while fetching config from config drive (cidata)`.
No SSH key, no hostname, no units — yet the machine boots fine.

**Cause:** FCOS's `proxmoxve` ignition platform reads its config from the
**cloud-init cidata drive**. `fw_cfg` alone is *not* the documented channel and does not
replace it. Without `--ide2 <storage>:cloudinit` there is no cidata device to read.

**Fix:** `--ide2 <storage>:cloudinit` **plus** `--cicustom vendor=<storage>:snippets/<file>`.
`vendor` is the correct slot — `qm cloudinit dump <vmid> vendor` is *not* a valid subcommand
(only `user`, `network`, `meta`), so verify by mounting the ISO:

```bash
mount -o ro /dev/zvol/pool/vm-<vmid>-cloudinit /mnt
cat /mnt/vendor-data | head -c 200     # should be your ignition JSON
```

### 2a. The `ignition.firstboot` marker

The proxmoxve image's GRUB `05_ignition.cfg` only puts `ignition.firstboot` on the kernel
cmdline while `/ignition.firstboot` exists on the extboot partition (p3). The image ships
*with* it, and `coreos-ignition-firstboot-complete` deletes it after the first run. So:

- Fresh image → Ignition runs once. Correct.
- Re-arming it (`touch` on p3) makes Ignition run again — **but see §5, this is a trap.**

---

## 3. Firmware: do not use OVMF

**Symptom:** `WARN: no efidisk configured! Using temporary efivars disk.` on every boot,
plus assorted boot weirdness.

**Cause:** I added `--bios ovmf` because "FCOS wants UEFI". It does not — the proxmoxve
artifact ships a BIOS boot partition (GPT p1) and the upstream recipe uses the **SeaBIOS
default**. Adding OVMF without a persistent `efidisk0` gives you throwaway NVRAM each boot.

**Fix:** no `--bios`, no `--machine q35`, no `efidisk0`. Also use `virtio-scsi-pci`, not
`virtio-scsi-single`.

---

## 4. Egress checks: never use `curl -f`

**Symptom:** `fleet-provision: no ghcr.io egress` after 6 minutes, on a machine with working
internet.

**Cause:** `curl -fsSI https://ghcr.io` and `curl -fsS https://ghcr.io/v2/` both fail,
because ghcr.io answers **405** on `/` and **401** on `/v2/`. `-f` turns any 4xx into a
non-zero exit.

**Fix:** reachability only — no `-f`:

```sh
curl -sS --max-time 10 -o /dev/null https://ghcr.io/v2/ >/dev/null 2>&1
```

This still fails on DNS/TLS/connect errors, which is the actual thing being tested.

Related: `rpm-ostree rebase` on stock FCOS requires **`--bypass-driver`** —
updates are owned by Zincati, and without the flag it refuses with
`error: Updates and deployments are driven by Zincati (zincati.service)`.

---

## 5. Do NOT clone the golden template (the biggest dead end)

**Symptom:** a clone boots to the emergency shell with
`useradd: group 'adm' does not exist` / `'wheel' does not exist`, and
`Ignition failed: failed to create users/groups`.

**Cause:** two things compounding.

1. On a cloned disk, FCOS's **`coreos-gpt-setup`** regenerates the root filesystem UUID and
   rewrites the bootloader *mid-boot*. `/sysroot` is then half-mounted when Ignition runs, so
   `useradd --root /sysroot` reads a group database that isn't there.
2. **Ignition is one-shot per system by design.** Re-arming `ignition.firstboot` to give each
   clone its own config makes it replay, and the log says so plainly:
   `WARNING: Ignition previously ran ... Ignition is not designed to run more than once per system.`

**Fix:** don't clone. Import the image per node (`--scsi0 <storage>:0,import-from=<qcow2>`) so
every node gets a genuine first boot. This costs ~1 min/node of copy and works.

Consequences worth remembering:

- Masking `coreos-ignition-unique-boot` is **not** sufficient; `coreos-gpt-setup` is the one
  that rewrites the root UUID.
- `qm clone --full` also thick-provisions 40 GiB per node (`refreservation`), so cloning was
  both wrong *and* wasteful. `import-from` lands ~3 GiB used per zvol.

---

## 6. Ignition `enabled: true` is not what you think

**Symptom:** a unit declared with `enabled: true` and `contents:` never runs;
`systemctl is-enabled` says `disabled`, and `/etc/systemd/system-preset/20-ignition.preset`
exists instead of a `.wants` symlink.

**Cause:** on a *re-run*, Ignition writes a systemd **preset**, and nothing applies presets at
boot time.

**Fix:** create the symlink explicitly:

```yaml
storage:
  links:
    - path: /etc/systemd/system/multi-user.target.wants/myunit.service
      target: /etc/systemd/system/myunit.service
```

---

## 7. Units that rebase must be idempotent

**Symptom:** node reboots forever, no network.

**Cause:** a oneshot that unconditionally does `rpm-ostree rebase` then `systemctl reboot`
runs again on every boot. After the first run the rebase is a no-op and the reboot is not.

**Fix:** guard at the top of the post-rebase branch:

```sh
if rpm-ostree status --booted 2>/dev/null | grep -q 'ublue-os/ucore'; then
  systemctl enable --now cockpit.service qemu-guest-agent.service 2>/dev/null || true
  exit 0
fi
```

---

## 8. Cockpit on uCore is a Podman container

uCore ships the Cockpit **RPMs** (`cockpit-bridge`, `cockpit-system`, `cockpit-files`,
`cockpit-networkmanager`, `cockpit-podman`, `cockpit-storaged`, `cockpit-selinux`) but
deliberately **not** `cockpit-ws`. `cockpit.service` runs `quay.io/cockpit/ws:latest` as a
container. It is **disabled by default**.

```sh
sudo systemctl enable --now cockpit.service   # then https://<ip>:9090
```

- Do not test with `rpm -q cockpit` — that package is absent by design.
- `cockpit-files` is the file manager for the ops class; no extra layering needed.
- It cannot be enabled from Ignition on stock FCOS, because the unit only exists after the
  rebase. Enable it from the provisioning script's post-rebase branch.
- Not pinned to a digest, by decision: uCore should pivot its own updates.

---

## 9. PVE 9.2 CLI notes

| Thing | Reality |
|---|---|
| VMID namespace | shared with LXC — an LXC CT with the same ID blocks `qm` commands silently |
| Import | `qm set <vmid> --scsi0 "<storage>:0,import-from=<file>"` (the old `scsi0=...,import=` form is gone) |
| `qm clone` + `--storage` | `parameter 'storage' not allowed for linked clones` |
| Storage stanza | `content images,snippets` — a bare `snippets 1` line makes PVE reject the whole section with `unable to parse value of 'snippets'` |
| Storage header | `type: name` (e.g. `dir: fleet`), not `name: type` |
| Guest agent | `--agent enabled=1` needs a **cold QEMU start**; a guest reboot does not add the virtio-serial port |
| `qm agent <id> ping` | useful liveness probe that does not depend on the guest network |

---

## 10. Networking: PVE cloud-init net config is ignored by FCOS

**Symptom:** `--ipconfig0` is set but the guest has no address.

**Cause:** PVE's `network-config` names the NIC `eth0`; the virtio device is `ens18`. FCOS has
no cloud-init agent to reconcile it.

**Fix:** set the address in the guest with NetworkManager (`nmcli con add ... ipv4.method manual`).
Note the interface name is `ens18`, and `nmcli -t -f DEVICE device` also lists `podman0` and
`veth*`, so filter on type:

```sh
nmcli -t -f DEVICE,TYPE device status | awk -F: '$2=="ethernet"{print $1; exit}'
```

---

## 11. Where per-node identity lives

`/etc` on the *deployment* (`/ostree/deploy/<os>/deploy/<hash>.0/etc`) is the **immutable image
copy**; writing there from the host does nothing. Only the running system (or Ignition) sees the
result. This cost hours. If you must touch a stopped disk, the writable state is under
`/ostree/deploy/<os>/var`, not the deployment's `/etc`.

Consequence: per-node hostname/IP/keys are handled by Ignition at first boot
(`templates/fleet.ign.butane`), never by host-side disk surgery.

---

## 12. Verification

`scripts/spike/50-verify.sh` — non-destructive, exit 0 on pass:

```
PASS: booted deployment: ostree-unverified-registry:ghcr.io/ublue-os/ucore:stable-20261122
PASS: static IP 10.10.10.99/24
PASS: qemu-guest-agent responds
PASS: 2 deployments present (rollback available)
PASS: cockpit RPMs present (cockpit-files-...)
PASS: cockpit :9090 container serving (HTTP 200)
```

Rollback was exercised by hand on 190: `rpm-ostree rollback --reboot` → FCOS → rollback → uCore.
Both directions clean, both deployments retained. Add `--rollback` to the verify script to repeat it
(two guest reboots, ~4 min).

---

## 13. Lesson

FCOS/uCore is **immutable and one-shot**. Fight that (host-side disk edits, replaying Ignition,
cloning a booted machine) and you get emergency shells that look like hardware faults. Work with
it (image + Ignition + import per machine) and each step is boring and repeatable.
