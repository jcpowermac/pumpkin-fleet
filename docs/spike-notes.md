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

---

## 14. Fleet OS image (BlueBuild)

Built by `.github/workflows/os.yml` from `recipes/fleet.yml`, following
`github.com/jcpowermac/os`. Published as **`ghcr.io/jcpowermac/pumpkin-os`**
(tags: `latest`, `45`, `<sha>-45`, `<date>-45`), public — anonymous pull, no
credentials on the nodes.

**Why an image instead of `rpm-ostree install sssd`:** layering is per-node drift.
One recipe is answerable from git.

### What it actually adds

Only what uCore is missing. Verified on `lobby` (201) after rebase:

```
sssd-2.13.2  sssd-ldap-2.13.2  sssd-tools  oddjob-mkhomedir-0.34.7  qemu-guest-agent
```

### BlueBuild specifics

- **Use `type: dnf`, not `type: rpm-ostree`.** The rpm-ostree module aborts in a
  container build: `error: This system was not booted via libostree`.
- **BlueBuild signs by default** and hard-fails without a keypair:
  `Unable to find private/public key pair`. Pass `build_opts: --no-sign` until a
  `SIGNING_SECRET` + `cosign.pub` + `- type: signing` exist.
- Keep the `mkdir -p /var/lib/rpm-state` script: rpm `%pre` scriptlets `touch` there
  and ostree container roots have an empty `/var`.

### uCore already ships the Cockpit sshd drop-in — do not add another

I claimed twice that uCore lacks `Include /etc/ssh/sshd_config.d/*.conf`. Both were
wrong: the grep ran as `core`, failed with `Permission denied`, and the empty output
read as absence. With `sudo`:

```
/etc/ssh/sshd_config:15:            Include /etc/ssh/sshd_config.d/*.conf
/etc/ssh/sshd_config.d/90-enable-localhost-passwords.conf:
  # uCore modification
  # enables Cockpit web login without exposing password auth to network
  Match Address 127.0.0.1,::1
    PasswordAuthentication yes
```

That is exactly what the Cockpit docs tell you to write, already present. Confirm with:

```sh
sudo sshd -T -C addr=127.0.0.1,user=core | grep -i ^passwordauthentication   # yes
sudo sshd -T -C addr=10.10.10.1,user=core | grep -i ^passwordauthentication  # no
```

**Lesson: any `grep` on `/etc/ssh/sshd_config` or `/etc/sssd/**` must run with `sudo`,
and a permission failure is not evidence of absence.**

### Rebasing a node onto it

```sh
sudo rpm-ostree rebase --bypass-driver \
  ostree-unverified-registry:ghcr.io/jcpowermac/pumpkin-os:latest
sudo systemctl reboot
```

**`rpm-ostree` no-ops when the ref string is unchanged** (`error: Old and new refs are
equal`). To pick up a new build of the same tag, rebase by digest:

```sh
ostree-unverified-registry:ghcr.io/jcpowermac/pumpkin-os@sha256:<digest>
```

### HAZARD: `fleet-provision` will revert a pumpkin-os node

`templates/fleet.ign.butane`'s guard is
`rpm-ostree status --booted | grep -q 'ublue-os/ucore'`. A node booted on
`ghcr.io/jcpowermac/pumpkin-os` does **not** match, so the unit would rebase it back to
uCore and reboot — a loop. Nodes must have `fleet-provision` disabled before rebasing
(done on 201), and the guard needs widening to `ublue-os/ucore|pumpkin-os` before new
nodes are created against this image.

---

## 15. authentik LDAP: the bind flow is the provider's `authorization_flow`

Full story in `docs/ldap-handoff.md` §10. The one-liner that will trip you up again:

**The Go LDAP outpost executes the provider's `authorization_flow` on every bind — it
never reads `authentication_flow`.** Server side: `LDAPOutpostConfigSerializer` sets
`bind_flow_slug = authorization_flow.slug` (fetched by the outpost at
`/api/v3/outposts/ldap/`).

- Symptom when misconfigured: bind "passes" (empty auth flow = passes for anyone), then
  the outpost's `GET /api/v3/core/users/me/` 403s (`auth_via: unauthenticated` in the
  `authentik-server` log) → `ldap_bind: Operations error (1)` + outpost log
  `failed to get user info`. Wrong passwords also pass, until you catch it.
- Current state (2026-11-23, verified working): `fleet-ldap` provider (pk 1) has
  **both** `authentication_flow` and `authorization_flow` = `ldap-bind`
  (identification → password → user_login, `authentication: none`,
  `policy_engine_mode: all`). `ldapsearch` over `ldaps://127.0.0.1:6636` returns
  `result: 0 Success`; wrong password → `Invalid credentials (49)`.
- Next phase (handoff §8): sssd on the node → `authselect select sssd` →
  `getent passwd jcallen` → Cockpit. `sssd.conf` does not exist yet, on purpose.

## 16. Running the ghcr images on the uCore game VMs (lobby 201)

The images build fine in Actions, but *consuming* them on the uCore nodes had
four independent landmines. All fixed in `tasks/gameserver.py`; this is why each
exists:

1. **`podman run -d` dies with the SSH session that started it.** Rootless podman
   runs in a user cgroup scoped to the session; logind tears it down on logout and
   kills the container (clean `Exited (0)`, no error).
   Fix: `sudo loginctl enable-linger core`. Symptom without it: container "Up" during
   the pyinfra run, `Exited (0)` seconds after `Disconnecting from hosts`.
2. **Data dir ownership must be set inside podman's uid namespace.** The container
   runs as uid/gid 1000, but podman for `core` remaps uids (subuid `524288`). A
   host-side `chown 1000:1000 /srv/<node>/data` makes the dir *root-owned inside the
   container* → entrypoint `mkdir /data/plugins: Permission denied` loop.
   Fix: `podman unshare chown -R 1000:1000 /srv/<node>/data`.
3. **`--network=host`, not the default pasta.** Rootless default netns (pasta with
   `--config-net`) gave the guest the *host's own IP* (10.10.10.54) and the
   host→guest forward never delivered: TCP connects, handshake times out, and
   `cat /proc/net/tcp` inside the guest shows only the LISTEN socket, never an
   ESTABLISHED one. `--network=host` sidesteps it entirely (one server per VM).
4. **firewalld (FedoraServer zone) drops the game port.** uCore enables firewalld
   with only ssh/cockpit open; the port bind lands on the host interface and is
   dropped. Fix: `firewall-cmd --add-port=25565/tcp --permanent && reload`.
   Does not survive an OSTree rebase — revisit for Task 7 Step 3.
5. **The game container runs as `core`, so cockpit only shows it in a PRIVILEGED
   session.** Rootless podman puts it in core's store
   (`/run/user/1000/podman/podman.sock`); jcallen (uid 2007) has an empty store of his
   own. cockpit-podman finds *other users'* containers by scanning
   `/sys/fs/cgroup` for `libpod.*scope`/`podman-*.scope`, and that scan is
   `superuser: require` — so with a non-privileged session the panel is simply empty.
   **Do not conclude the grant is broken:** verify `sudo -l -U jcallen` lists
   `/usr/bin/cockpit-bridge`, `systemctl --user is-active podman.socket` (as core) is
   active, then click **Administrative access** in cockpit and confirm with the
   LDAP password (the deliberate no-NOPASSWD model). Privileged cockpit = root
   bridge, but it still sees core's containers via that cgroup scan — root's own
   podman store holds only cockpit's `ws` container, which is why checking
   `sudo podman ps` disproves nothing.
   **The scan runs once, in `componentDidMount`, and nothing re-runs it when the
   session is later elevated** (cockpit-podman 132 only listens for
   `locationchanged`). Elevating while the Podman page is open leaves it empty —
   **acquire Administrative access first, then reload the Podman page**; that is
   the whole fix (confirmed 2026-11-25 on vanilla: after the reload the panel lists
   `vanilla`). The two silent outcomes go to the *browser* console, not the server:
   `unprivileged session, skipping detection of other users` vs `getInfo failed`
   (on any error the user is dropped from the list with no UI message).

   Before treating it as a node fault, note that lobby and vanilla measure
   **identically** on every input the plugin uses — same unit
   (`-v /:/host --pid host --privileged`), same scan output (`1 1000 core`), same
   container cgroup shape, same socket ownership, same sudo grant, no AVCs. There is
   nothing to diff between machines; the state that decides this lives in the
   browser.

   Verified on vanilla 2026-11-25, so the daemon side can be excluded in one shot
   (as root, all should be 200):
   ```sh
   sudo find /sys/fs/cgroup \( -name 'libpod.*scope' -o -name 'podman-*.scope' \) \
        -exec stat --format='%u %U' {} \;      # -> 1000 core
   sudo curl -s -o /dev/null -w '%{http_code}\n' \
        --unix-socket /run/user/1000/podman/podman.sock \
        http://d/v5.0.0/libpod/containers/json?all=true
   ```

**Verifying a server is really up:** `scripts/mc-ping.py <ip> 25565` does a real
protocol-777 status ping (varint framing; the server never acks the handshake — it
waits straight for the status request; status response is
`[packet id][string len][JSON]`). A plain TCP connect is not enough: pasta accepts
the SYN before it's proven it forwards.
