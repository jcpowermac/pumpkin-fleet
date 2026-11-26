# Pumpkin Fleet Deployment Plan (pve3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the full Minecraft fleet on pve3: 4 uCore game-server VMs, Velocity + Traefik + authentik infrastructure, then migrate the existing 7 old LXCs off.

**Architecture:** Game nodes are **uCore (FCOS-based, OSTree/immutable) KVM VMs** — one golden VM built from the FCOS live ISO + `ucore-autorebase` ignition, then PVE-cloned into 4 servers. Infra nodes stay LXC (existing Velocity/Traefik containers, new authentik from community-scripts). All configuration is driven by a pyinfra repo. Single host: pve3 (SPOF accepted — homelab, not mission critical).

**Tech Stack:** Proxmox VE 9.2 (pve3), uCore `stable` stream, pyinfra, community-scripts authentik 2026.8.6, Traefik, Velocity (NeoForge fabric connector), OPanel (in-process), systemd.

**Spec:** `docs/superpowers/specs/2026-11-21-pumpkin-fleet-lxc-pyinfra-design.html`

**Design change vs spec (user decision 2026-11-21):** the spec said all-LXC with uCore rootfs. The uCore-in-unprivileged-LXC spike hit PVE 9 LXC setup friction (`/dev` mount EPERM after idmap/hwattr fixes); user chose **uCore as VMs** for game nodes instead. Infra LXCs unchanged.

## Global Constraints

- All fleet resources on **pve3** (172.31.12.99) only. No pve1/pve2.
- VLAN 45 bridge `vmbr1`, subnet `10.10.10.0/24`, gateway `10.10.10.1`. DHCP owns `.200–.220`; all statics below `.200`.
- **IP plan (fixed):** Traefik `.10`, Velocity `.52`, authentik `.53`, lobby `.54`, plugins `.55`, vanilla `.56`, NeoForge `.57`. Spike VM uses `.99` (temporary).
- **VMID allocation:** 190 = spike/golden uCore (becomes template), 201 = lobby, 202 = plugins, 203 = vanilla, 204 = NeoForge. 150 = authentik LXC. Existing: 101 velocity, 106 traefik (LXC).
- uCore stream: `ghcr.io/ublue-os/ucore:stable` (tag at spike time: `stable-20261122`; always pin the exact tag the golden VM ends on and record it).
- NeoForge server: 4 GiB heap, Velocity protocol via NeoForge Connector (fabric), server port 24567.
- Omada edge forwards (only two): 443 → `10.10.10.10` (Traefik), 24587 → `10.10.10.52` (Velocity). authentik is **not** publicly exposed.
- authentik is the sole IdP; OPanel authenticates via OIDC against it; ops access (Cockpit/SSH) via sssd + LDAPS outpost on authentik.
- Game servers are **pets**: long-lived, pinned versions, rollback-able OS updates. No golden-template drift policy beyond the initial clone.
- Laptop SSH to pve3 works: `ssh root@172.31.12.99` (key `jcallen-mac` installed in `/root/.ssh/authorized_keys`). API tokens in `~/Development/homelab/.pi/mcp.json` are **stale**; prefer SSH for this plan.
- Existing pve3 LXC state: 100 sr, 101 velocity (.20, running), 102 usc (running), 103 CMCS (stopped), 104 pangolin, 105 cmcs2, 106 traefik (no IP), 107 lobby (.30, running). No QEMU VMs exist yet.
- pve3 storage: `local-zfs` = zpool `rpool/data` (453 GB, 38% used); `pool` = zpool `pool` (1.9 TB, 6% used). LXC rootfs go on `local-zfs`. **VM disks go on `pool`** (plenty of space; keeps LXC pool free).
- pve3 has no podman/umoci by default (umoci was installed at `/usr/local/bin/umoci` during the spike — harmless to leave).

## Review Focus

Failure modes the spec implies but no single step pins down:

1. **OVMF/UEFI firmware missing on pve3** (no VMs ever ran here) — FCOS wants UEFI; a BIOS-boot attempt wastes the spike. → Task 1 Step 4 checks firmware before creating the VM.
2. **VM needs egress to `ghcr.io` during first-boot rebase** — no internet on vmbr1 = broken first boot, easy to misread as a uCore failure. → Task 1 Step 2 verifies VM-side reachability of the registry.
3. **IP collisions while migrating** (101 velocity holds .20, spike holds .99, DHCP .200–.220) — a duplicate IP breaks a running game server silently. → Task 5 and Task 8 each verify the subnet before flipping.
4. **Immutable rootfs vs game data** — Pumpkin/NeoForge worlds, saves and configs die on every OS update if written to `/`. → Task 7 Step 3 pins bind-mounted data dirs.
5. **authentik LXC recovery** — it's a source build (no package reinstall); if the LXC dies, only a pg_dump + script re-run restores it. → Task 3 Step 6 exercises exactly that restore path once.

---

### Task 1: Spike — golden uCore VM on pve3 (VMID 190)

> **EXECUTED 2026-11-22 — PASS, but Steps 2–6 below are SUPERSEDED.**
> The live-ISO path was never used. Do not follow them; the working recipe is:
> FCOS **proxmoxve qcow2** → `qm set --scsi0 "pool:0,import-from=<qcow2>"` →
> `--ide2 pool:cloudinit` + `--cicustom vendor=fleet:snippets/<ign>` → **SeaBIOS** (NOT OVMF).
> Full write-up with every failure mode: `~/Development/pumpkin-fleet/docs/spike-notes.md`,
> summarised in `docs/superpowers/notes/2026-11-21-ucore-vm-spike.md`.
> Implemented by `~/Development/pumpkin-fleet/scripts/fleet/20-create-nodes.sh`.

**Files:**
- On pve3: `/var/lib/vz/images/fcos-stable-live.iso` (ISO storage `local`), ignition built inside the FCOS live environment
- Local: `docs/superpowers/notes/2026-11-21-ucore-vm-spike.md` (spike results: exact ISO build, uCore tag, rebase timings, anything gnarly)

**Interfaces:**
- Produces: **VM 190 turned into a PVE template** (uCore, hostname `ucore-golden`, static `10.10.10.99`, disk on `pool`) — but Task 6 must NOT clone it (see Task 6); it is a reference image only. Plus the pinned uCore deployment tag recorded in the spike notes.

- [ ] **Step 1: Clean up the LXC spike leftovers on pve3**

Run (via `ssh root@172.31.12.99`):
```bash
pct stop 190 2>/dev/null; pct destroy 190 --force 2>/dev/null
rm -f /etc/pve/nodes/pve/lxc/190.conf /etc/pve/lxc/190.conf
zfs destroy rpool/data/subvol-190-disk-0 2>/dev/null
rm -rf /tmp/ucore-store /tmp/ucore-rootfs /tmp/ucore-bundle2 /tmp/dh /tmp/ls*.strace /tmp/lxc19*.log /tmp/ucore-*.log
```
Verify: `pct list | grep 190` empty, `zfs list | grep 190` empty, `df -h /tmp` free space grew ~4 GB. (This frees the VMID 190 for the VM.)

- [ ] **Step 2: Download the FCOS stable live ISO and verify VM-side registry reachability**

```bash
# latest stable build metadata
BUILD=$(curl -fsSL https://builds.coreos.org/prod/streams/stable.json | python3 -c 'import sys,json;print(json.load(sys.stdin)["builds"]["x86_64"]["ostree_container"]["oci"]["url"].rsplit("/",1)[0].split("/releases/")[1].split("/")[0])')
echo "build: $BUILD"   # record this in spike notes
curl -fL -o /var/lib/vz/images/fcos-stable-live.iso \
  "https://builds.coreos.org/prod/releases/$BUILD/x86_64/x86_64-live.iso"
```
Verify: file is ~1.5 GB and `file /var/lib/vz/images/fcos-stable-live.iso` says ISO 9660. Then, to pre-check egress for the rebase (failure mode #2), temporarily create a scratch check: boot isn't possible yet, so instead confirm from pve3 that `curl -fsSI https://ghcr.io/v2/` succeeds (host egress) and note that the VM will use the same bridge.

- [x] **Step 3: UEFI firmware (OBSOLETE — do not do this)**

~~Check OVMF exists~~ — **wrong premise.** FCOS's proxmoxve artifact has a BIOS boot partition
and upstream uses the SeaBIOS default. Adding `--bios ovmf` without a persistent `efidisk0` gives
throwaway NVRAM on every boot (`WARN: no efidisk configured! Using temporary efivars disk`) and
buys nothing. Create the VM with **no** `--bios`, no `--machine q35`, no `efidisk0`, and
`--scsihw virtio-scsi-pci` (**not** `virtio-scsi-single`).

- [ ] **Step 4: Create VM 190 (SUPERSEDED — see banner at top of Task 1)**

> Superseded: do not use `--cdrom`/live ISO/`--bios ovmf`. Use the qcow2 import +
> cloud-init cidata recipe in `~/Development/pumpkin-fleet/docs/spike-notes.md` §1.
> Steps 4–6 are kept only as a record of the original plan.

**Original text (obsolete):**

- [ ] **Step 4: Create VM 190 on the ISO**

```bash
qm create 190 --name ucore-spike --memory 4096 --cores 4 --cpu host \
  --net0 virtio,bridge=vmbr1 --machine q35 --bios ovmf \
  --scsihw virtio-scsi-pci --agent enabled=1 --tablet 1 --vga serial0 \
  --disk0 pool:40,format=qcow2,iothread=1 \
  --cdrom local:iso/$(basename /var/lib/vz/images/fcos-stable-live.iso),media=cdrom
qm set 190 --ipconfig0 ip=10.10.10.99/24,gw=10.10.10.1 --cores 4
qm start 190
```
(If the ISO was placed under `/var/lib/vz/images/` but PVE's ISO storage `local` points elsewhere, copy it to the storage's ISO dir first — check `pvesm status`/storage dir. The `-cdrom` reference must resolve in PVE.)

Verify: `qm console 190` (or `qm monitor`) shows GRUB; select "Fedora CoreOS Live"; login as `core` (passwordless or `changeme` depending on build).

- [ ] **Step 5: Install with the uCore auto-rebase ignition (inside the live env)**

In the VM console (or scp over the live network — the live env gets DHCP on vmbr1):
```bash
# in the live env, as core:
curl -fL -o ucore-autorebase.butane \
  https://raw.githubusercontent.com/ublue-os/ucore/main/examples/ucore-autorebase.butane
# replace password_hash: YOUR_GOOD_PASSWORD_HASH_HERE with an ssh_authorized_keys entry
# containing the laptop's ed25519 pubkey (read the template first; keep the template's user name)
sed -i 's/YOUR_GOOD_PASSWORD_HASH_HERE//; ...'  # or edit in vi; then:
butane -o ucore-autorebase.ign ucore-autorebase.butane   # butane ships in the live env
sudo coreos-installer install /dev/nvme0n1 --ignition-file ucore-autorebase.ign \
  || sudo coreos-installer install /dev/vda --ignition-file ucore-autorebase.ign
```
(`coreos-installer install` also accepts `--to-disk /dev/vda`. Target = the PVE disk, not the ISO's loop device — `lsblk` to confirm.)

Verify: installer prints a success summary; `sudo systemctl reboot`.

- [ ] **Step 6: Ride out the two-stage rebase**

Detach ISO after first reboot completes: `qm set 190 -cdrom none`, or wait and `qm stop 190 && qm set 190 -cdrom none && qm start 190`. First boot pulls `ucore:stable` (unsigned reference), reboots; second boot rebases to the signed reference, reboots again. Total ~10–20 min.

Verify: `ssh core@10.10.10.99 'hostnamectl && rpm-ostree status'` — os-release says uCore (Universal Blue), status shows the uCore deployment booted. Record the exact deployed tag (e.g. `stable-20261122`) in the spike notes.

- [ ] **Step 7: Verify cockpit, guest agent, and the rebase/rollback cycle**

```bash
# cockpit (uCore runs cockpit; expect HTTP 302/200)
curl -ksI https://10.10.10.99:9090 | head -3
# guest agent
ssh root@172.31.12.99 'qm gueststatus 190'   # expect agent_running: 1
# rollback cycle: boot the previous deployment, come back
ssh core@10.10.10.99 'sudo rpm-ostree rollback && sudo systemctl reboot'
# after it returns:
ssh core@10.10.10.99 'rpm-ostree status'     # previous deployment booted
ssh core@10.10.10.99 'sudo systemctl.reboot' # back to latest via `rpm-ostree rollback` again (or deploy)
```
Verify: all three pass. If rollback never boots back, that's a **kill** for the pet-model OS story (see Step 9).

- [ ] **Step 8: If it passed — make 190 the golden template**

```bash
ssh core@10.10.10.99 'hostnamectl set-hostname ucore-golden'
ssh root@172.31.12.99 'qm template 190'
```
Verify: `qm list | grep 190` shows `template`; `qm template 190` succeeded without warnings about the agent.

- [ ] **Step 9: Record spike outcome**

Write `docs/superpowers/notes/2026-11-21-ucore-vm-spike.md`: ISO build ID, final uCore tag, rebase timings, OVMF package name, any console quirks, **PASS/FAIL**. **Kill criteria (user-approved):** if a clean uCore VM did not boot and pass Steps 6–7 within one afternoon of attempts, FAIL — fallback fleet base is plain Fedora 44 VM (qcow2 from `coreos-installer download --stream stable --type qemu` won't do; use a Fedora 44 cloud/server image) + dnf + `dnf install cockpit`, and Tasks 6–7 re-pin to that base. Update the spec HTML's OS section accordingly.

---

### Task 2: pyinfra fleet repo baseline

**Files:**
- Create: `~/Development/pumpkin-fleet/` (new repo; prior art layout: `~/Development/crafty-pyinfra/`)
- Create: `pyproject.toml` (pyinfra dep), `inventory/hosts.py` (or `hosts.yml`), `tasks/ping.py`, `Makefile` or `justfile` with `up` target

**Interfaces:**
- Consumes: SSH to pve3 (key-based, root) and to VM/LXC hosts (core@… / root@… once they exist).
- Produces: `pyinfra up --inventory inventory/hosts` runs green against every host in the inventory; later tasks add files under `tasks/` and `configs/` in this repo and run `pyinfra up <host> <task>`.

- [ ] **Step 1: Scaffold the repo**

Copy the crafty-pyinfra layout (pyproject, inventory style, deploy runner) into `~/Development/pumpkin-fleet`. Inventory hosts (names used by all later tasks): `pve` (172.31.12.99), `ucore-golden` (10.10.10.99, user `core`), plus placeholders for `authentik` (10.10.10.53), `traefik` (10.10.10.10), `velocity` (10.10.10.52), `lobby` (.54), `plugins` (.55), `vanilla` (.56), `neoforge` (.57) — commented out until the tasks that create them.

- [ ] **Step 2: Add a `ping` task that proves connectivity**

`tasks/ping.py`: `ssh_ping` (pyinfra builtin) per host.

- [ ] **Step 3: Run and verify**

```bash
cd ~/Development/pumpkin-fleet
just up            # or: pyinfra up --inventory inventory/hosts ping
```
Expected: success for `pve` and `ucore-golden` (others skipped/commented). Commit.

---

### Task 3: authentik LXC (VMID 150, 10.10.10.53)

**Files:**
- On pve3: LXC 150 created by `https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/authentik.sh`
- Create (repo): `configs/authentik/authentik.env` (pinned script env vars, incl. `AUTHENTIK_VERSION=version/2026.8.6`)
- Create (repo): `tasks/authentik.sh` runner + `tasks/authentik_health.py`

**Interfaces:**
- Produces: authentik on `http://10.10.10.53:9000` (internal only), admin credentials in the repo's secrets dir (git-crypt or age, same scheme crafty-pyinfra uses), LDAP outpost listening on `:636` (LDAPS), `pg_dump` backup at `/var/lib/authentik/backups/authentik.sql.gz` refreshed nightly.

- [ ] **Step 1: Run the community script with pinned env**

```bash
curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/authentik.sh -o /tmp/authentik.sh
# edit /tmp/authentik.sh defaults or export: VMID=150, IP=10.10.10.53/24, GATEWAY=10.10.10.1,
# BRIDGE=vmbr1, CORES=4, MEMORY=8192, DISKSIZE=16, AUTHENTIK_VERSION=version/2026.8.6
bash /tmp/authentik.sh
```
(The script creates an unprivileged Debian 13 LXC, builds authentik from source incl. the LDAP outpost, installs systemd units. This takes ~20–40 min — run under `screen`/`tmux` on pve3.)

- [ ] **Step 2: Set static IP and verify**

The script may configure DHCP/static per its own logic — ensure final state: static `10.10.10.53/24` gw `10.10.10.1`.

Verify: `curl -s http://10.10.10.53:9000/api/v3/health/` returns 200 JSON; log in as the script's admin (password printed at install → move to secrets, rotate); `ss -ltnp | grep 636` shows the LDAPS outpost.

- [ ] **Step 3: Create fleet OIDC provider + group**

In the authentik web UI (via `http://10.10.10.53:9000` from the LAN, not public): provider "pumpkin-oidc" (client ID/secret recorded in secrets), application + group `mc-admins` (5–10 users will join later via self-service).

Verify: OIDC discovery `http://10.10.10.53:9000/application/o/pumpkin-oidc/endpoints/` (or the configured client id) returns JSON with `authorization_endpoint`.

- [ ] **Step 4: Nightly pg_dump**

`systemd` timer on the LXC: daily `pg_dump -Fc` → `/var/lib/authentik/backups/`, keep 14.

- [ ] **Step 5: Wire into pyinfra**

Uncomment `authentik` in inventory; add `tasks/authentik_health.py` (health endpoint + 636 listener check).

Verify: `just up` green for authentik.

- [ ] **Step 6: Exercise the recovery path once (failure mode #5)**

`pg_dump` to file now, `pct stop 150`, restore the dump into a **scratch** database (or a fresh temporary LXC if cheap), confirm row counts, tear down scratch. Record the exact restore commands in the repo (`configs/authentik/RECOVERY.md`).

---

### Task 4: Traefik (LXC 106) → 10.10.10.10 + Omada

**Files:**
- On pve3: `/etc/pve/lxc/106.conf` (IP added)
- Create (repo): `configs/traefik/dynamic.yml` (routes/middleware), `tasks/traefik.py` (copies config, reloads)

**Interfaces:**
- Consumes: authentik OIDC endpoints (Task 3), Omada edge (user has console access).
- Produces: public 443 → Traefik; route `auth.<zone>` → `http://10.10.10.53:9000` (authentik's only public face); placeholder game-panel routes for Task 7 (one per server: `<server>.<zone>` → that VM's OPanel port, OIDC middleware).

- [ ] **Step 1: Give 106 its IP and verify it's up**

```bash
pct set 106 --net0 "name=eth0,bridge=vmbr1,ip=10.10.10.10/24,gw=10.10.10.1"  # match existing net0 line, add ip
pct start 106; sleep 5; pct exec 106 -- systemctl status traefik --no-pager | head -5
```
Verify: `curl -sk https://10.10.10.10/` reaches Traefik (dashboard/404 page), Traefik's entrypoint is 443/TLS (existing ACME/cert setup — inspect its current config first and preserve it).

- [ ] **Step 2: Add the auth route + OIDC middleware**

`configs/traefik/dynamic.yml`: route for `auth.<zone>` (zone = user's DDNS/public zone — **ask user for the exact zone name before writing**, design §9 open item) → authentik; OIDC middleware (`plugin` or `middlewares.authentik` per current Traefik version) for the future panel routes.

Verify: `curl -sk https://auth.<zone>/api/v3/health/` returns authentik health through Traefik; hitting a panel placeholder route redirects to the authentik login page.

- [ ] **Step 3: Omada edge forward**

Add forward 443 → `10.10.10.10` (24587 comes in Task 5). Verify from outside: `curl -skI https://auth.<zone>` succeeds.

---

### Task 5: Velocity (LXC 101) → 10.10.10.52

**Files:**
- On pve3: `/etc/pve/lxc/101.conf` (IP change .20 → .52)

**Interfaces:**
- Consumes: running Velocity LXC (currently at `10.10.10.20`).
- Produces: Velocity at `10.10.10.52`, port 24587 (its own game port unchanged internally; Omada forwards 24587→.52), NeoForge fabric-connector listener active.

- [ ] **Step 1: Check for IP collisions, then move the IP (failure mode #3)**

```bash
ssh root@172.31.12.99 'arp-scan -I vmbr1 10.10.10.52 2>/dev/null || nmap -sn 10.10.10.52'  # expect: nobody home
```
Verify .52 is free. Then: `pct stop 101`, set `ip=10.10.10.52/24` in net0 (keep hwaddr), `pct start 101`, verify `pct exec 101 -- systemctl status velocity` (or its actual unit) is running. **Note:** 101 is a *running game server* (velocity, port 20 in use by players?) — do the stop/restart in a player window; confirm with user which old servers are player-visible before stopping.

- [ ] **Step 2: Verify the Velocity config matches the fleet plan**

Inspect Velocity.toml: forward to the 4 new game-server IPs once they exist (Task 6/7 add them); protocol `fabric` support via the connector for NeoForge. Until servers exist, Velocity may not start cleanly with missing forwards — that's fine, it just needs to be running and reachable.

- [ ] **Step 3: Omada forward 24587 → 10.10.10.52**

Verify from outside: `nc -vz <public-ip> 24587` connects (Minecraft ping optional).

---

### Task 6: Clone game-server VMs (201–204)

**Files:**
- On pve3: VMs 201 lobby, 202 plugins, 203 vanilla, 204 neoforge (clones of template 190)
- Create (repo): `tasks/ucore_clone.py` (qm clone + per-VM hostname/IP via pyinfra)

**Interfaces:**
- Consumes: template VM 190 (Task 1), pinned uCore tag from spike notes.
- Produces: 4 running uCore VMs with static IPs `.54/.55/.56/.57`, hostnames `lobby/plugins/vanilla/neoforge`, guest agent
  running, SSH key for `core`. **Plus a second disk per VM** (2026-11-26 decision): `zfs create -V <size>
  pool/mcdata/<node>` attached as `scsi1: /dev/zvol/pool/mcdata/<node>` — host-owned, so it survives
  `qm destroy --purge`. `scsi0` stays the disposable OS disk.

Sizing (fixed): 201/202/203 = 4 cores / 4 GiB; 204 = 4 cores / 6 GiB (4 GiB heap + JVM overhead). All: `pool` disk 40 GiB qcow2, virtio-net on vmbr1.

- [x] **Step 1: Create and boot (DONE — do NOT clone template 190)**

**Cloning template 190 is broken.** On a cloned disk FCOS's `coreos-gpt-setup` regenerates the
root filesystem UUID and rewrites the bootloader mid-boot, so Ignition's `useradd` runs against a
half-mounted `/sysroot` and the boot lands in the emergency shell:

```
Ignition failed: failed to create users/groups: failed to create user "core":
  useradd: group 'adm' does not exist ... 'wheel' does not exist
```

Ignition is one-shot per system by design. Create each node by **importing the image**, which
gives it a genuine first boot:

```bash
for pair in "201 lobby 10.10.10.54" "202 plugins 10.10.10.55" \
            "203 vanilla 10.10.10.56" "204 neoforge 10.10.10.57"; do
  set -- $pair
  qm create $1 --name $2 --cores 4 --memory 4096 --cpu host \
    --net0 virtio,bridge=vmbr1 --scsihw virtio-scsi-pci \
    --agent enabled=1 --tablet 1 --vga serial0 --serial0 socket
  qm set   $1 --scsi0 "pool:0,import-from=/var/coreos/images/fcos.qcow2"
  qm resize $1 scsi0 40G
  qm set   $1 --ide2 pool:cloudinit --boot order=scsi0
  qm set   $1 --cicustom vendor=fleet:snippets/$2.ign --ciupgrade 0
  qm set   $1 --ipconfig0 ip=$3/24,gw=10.10.10.1
  qm start $1
done
```

Implemented in `~/Development/pumpkin-fleet/scripts/fleet/20-create-nodes.sh`. Each node's
Ignition sets the static IP, rebases to uCore (`--bypass-driver`), and reboots (~5 min, parallel).

Verify: all 4 boot to SSH (key auth, user `core`), `qm gueststatus` shows agent running.

- [x] **Step 2: Per-VM hostname + static IP (DONE via Ignition, not pyinfra)**

> Handled at first boot by `templates/fleet.ign.butane` (hostname file + nmcli), because
> PVE's cloud-init `network-config` is ignored by FCOS (it names the NIC `eth0`; virtio is
> `ens18`). No pyinfra task needed for identity.

**Original text:**

- [ ] **Step 2: Per-VM hostname + static IP**

Via pyinfra `tasks/ucore_clone.py` (uCore uses NetworkManager): set hostname + `nmcli`/`systemd-networkd` static address per the IP plan; reboot each; re-verify SSH on the new IP.

Verify: `for ip in .54 .55 .56 .57` — `ssh core@10.10.10.<ip> 'hostname'` returns the right name; arp-scan shows no duplicates.

- [ ] **Step 3: Wire into pyinfra inventory**

Uncomment all four hosts; `just up` green.

---

### Task 7: Game workloads + OPanel + identity on the 4 VMs

**Files:**
- Create (repo): `tasks/gameserver.py` (workload install), `configs/<server>/` per-server pins (Pumpkin version + OPanel version, NeoForge version + mod list + connector version), `configs/` sssd + LDAPS config template

**Interfaces:**
- Consumes: 4 uCore VMs (Task 6), authentik OIDC + LDAPS (Task 3), Velocity (Task 5).
- Produces: each server running its game + OPanel (OIDC-protected), sssd auth for Cockpit/SSH via authentik, data dirs on bind mounts that survive OS rebases, Velocity forwards updated.

Workload mapping (from spec): lobby (`.54`) = Pumpkin + OPanel; plugins (`.55`) = Pumpkin/Paper-patched + OPanel (spec: "Pumpkin servers"); vanilla (`.56`) = Pumpkin vanilla + OPanel; NeoForge (`.57`) = NeoForge 1.x + OPanel mod + NeoForge Connector, `-Xms4g -Xmx4g`, Velocity protocol `fabric`. (If the spec's per-server game mapping differs, the spec HTML §server table wins — executor re-reads it before Step 1.)

- [ ] **Step 1: Pin versions**

Write the exact version pins into `configs/` (Pumpkin release tag, OPanel release ≥ 2.1.4 for OIDC, NeoForge build id, connector version). Ask user only for the NeoForge mod set (design §9 open item) — everything else is discoverable from upstream release pages; prefer the newest stable that matches the client version the players use.

- [ ] **Step 2: Install workloads (pyinfra, per host)**

On each VM: create service user, download pinned artifacts (Pumpkin: upstream install script with pinned tag; NeoForge: installer with pinned build), systemd unit with `Restart=always`, OPanel embedded (Pumpkin: `/opanel/config.json` + the in-process OPanel for Pumpkin; NeoForge: OPanel mod + connector mod in mods/).

Verify per server: game process running (`systemctl status`), game port answers a ping (`nc -zv 10.10.10.<ip> 25565` / 24567 for neoforge), OPanel web UI reachable internally (e.g. `http://10.10.10.<ip>:<panelport>/`) and unauthenticated (OIDC not yet configured).

- [ ] **Step 3: Bind-mount game data (failure mode #4)**

Per VM: `mkdir -p /var/<server>/{world,plugins,config}` (NeoForge: also `mods/`, `saves/`) + bind mounts (systemd `mount` units or fstab) into the workload dir; move world/config into them.

Verify: `findmnt` shows the binds; delete-and-recreate test: stop server, `touch /var/<server>/world/keepfile`, restart, file still there; then **run one `rpm-ostree` rebase + reboot on VM 201** and confirm the world survived (this is the pet-model acceptance test).

- [ ] **Step 4: sssd + LDAPS + Cockpit/SSH identity**

`dnf install sssd sssd-ldap realmd` (or `podman`-free plain install on uCore via `rpm-ostree` layering if direct install is blocked — uCore is immutable: use a **layered RPM** or a **user-built image**; record which in notes and apply the same way to all four), point at `ldaps://10.10.10.53:636` (authentik outpost, starttls per outpost config), CA from authentik.

Verify: `id <some-authentik-user>` resolves on each VM; SSH login as that user works; Cockpit login (via `https://<vm>:9090`) accepts an authentik user.

- [ ] **Step 5: OPanel OIDC + per-server user lists**

Configure OPanel OIDC (issuer = authentik provider from Task 3 Step 3, client per server or shared with per-server `Allowed-User-IDs` lists), Traefik panel routes from Task 4 Step 2 now point at these OPanel ports.

Verify: browser flow — panel URL → authentik login → back to panel; a user **not** in that server's list has no access; 5–10-user self-service: users can claim accounts in authentik (group join request) — confirm the group-request flow works for one test user.

- [ ] **Step 6: Update Velocity**

Add the 4 real servers to Velocity's `velocity.toml` (correct IPs/ports; NeoForge with fabric protocol), restart, `qm`-less check: `curl`/mc ping via Velocity `.52:24587` lists all 4.

Verify: from a test client (or `mcbbs`-style ping script) all 4 servers appear behind Velocity.

---

### Task 8: Migrate old servers off

> **DESCOPED 2026-11-26 by jcallen — do not touch the legacy LXCs (100–107).** No world migration, no copies out of
> them, no stopping them. Persistent Minecraft data instead lives on a second disk per VM (`scsi1`, host-owned ZFS volume
> `pool/mcdata/<node>`), see `docs/superpowers/specs/2026-11-26-fleet-plugin-gitops-design.md` §2b. Steps below are kept
> as history only; do not execute.

**Files:**
- Create (repo): `configs/migration-map.md` (old VMID → new host → data dirs → cutover window)

**Interfaces:**
- Consumes: everything above. Old servers: 100 sr, 102 usc, 103 CMCS, 104 pangolin, 105 cmcs2, 107 lobby (101/106 already handled).
- Produces: old LXCs stopped (or destroyed if user agrees), their worlds/players moved to the new fleet, no running VMID in the 100–107 range except where intentionally kept.

- [ ] **Step 1: Write and confirm the migration map**

Propose the mapping (executor drafts; **user confirms before any data copy** — design §9 open item): e.g. lobby 107 → new lobby 201; usc 102 / sr 100 / CMCS 103 / cmcs2 105 / pangolin 104 → fold into plugins/vanilla/NeoForge roles or retire (ask which worlds still have players). Record decisions in `configs/migration-map.md`.

- [ ] **Step 2: Migrate stopped servers first**

For each: snapshot old LXC (`pct snapshot`), copy world/config into the new VM's `/var/<server>/` bind dirs, update server UUID files if needed, test the new instance with the old world, then stop the old LXC.

- [ ] **Step 3: Migrate running servers in a player window**

For each still-running (player-visible) server: announce, snapshot, cut over (new VM takes the Velocity slot), verify world integrity (spawn/tp test), stop old LXC, keep snapshot 7 days.

- [ ] **Step 4: Final state check**

`pct list` + `qm list` match `migration-map.md`; arp-scan of `10.10.10.0/24` shows exactly the planned statics (failure mode #3); old-world backups verified restorable from the PVE backup job.

---

### Task 9: Backups + ops docs

**Files:**
- Create (repo): `tasks/backups.py`, `configs/ops/README.md` (runbook)

**Interfaces:**
- Consumes: final fleet state.
- Produces: nightly vzdump job (VMs 201–204 + LXC 150; store per user preference), ZFS auto-snapshot schedule on both pools, and a one-page runbook.

- [ ] **Step 1: vzdump job**

PVE backup job: nightly, VMs 201–204 + LXC 150 (+101/106), format zfs for LXCs / qemu for VMs, prune 7 daily / 4 weekly. (Backup destination: ask user — local `pool` has space; off-host send is a nice-to-have, not required by spec.)

- [ ] **Step 2: ZFS snapshots**

`zfs set com.sun:auto-snapshot=on` on `pool` and `rpool/data` (default schedule), or PVE's storage snapshot schedule if preferred.

- [ ] **Step 3: Runbook**

`configs/ops/README.md`: rebase procedure (`rpm-ostree update` + reboot, one VM at a time), rollback procedure (`rpm-ostree rollback`), authentik restore (from Task 3 Step 6), "rebuild a game VM from template 190 + restore /var" procedure, uCore pin + where it's recorded.

Verify: one test vzdump cycle completes; runbook steps each executed once (restore test from Task 3 already covers authentik).

---

## Self-Review Notes (executor may ignore)

- Spec coverage: 4 game servers (T6–T7), Velocity (T5), authentik (T3), Traefik+Omada (T4), OPanel+OIDC (T7), sssd/Cockpit (T7), backups (T9), migration (T8), pet-model rollback (T1 Step 7, T7 Step 3). OS base decision = T1.
- Open items that require the **user** mid-plan: public zone name (T4), NeoForge mod set (T7), migration map confirmation (T8), backup destination (T9), player-window scheduling (T5/T8).
