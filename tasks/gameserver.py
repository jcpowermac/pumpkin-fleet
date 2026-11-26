"""Fleet game containers: a quadlet unit per node, following an image channel tag.

The unit is the desired state and podman is the reconciler: `AutoUpdate=registry` makes
podman's own timer converge the node on whatever the channel tag points at, so a new
image arrives without an ssh session. Channels and the reboot window come from
configs/pins.env; the canary node (by name) follows the pre-promotion channel. Data dir
is bind-mounted at /data and lives on the host-owned zvol (scsi1), so it survives both an
OS rebase and a destroy/recreate of the VM.

Usage: uv run pyinfra inventory/hosts.py --limit lobby tasks/gameserver.py
"""
import base64
import importlib.util
import json
import sys
from pathlib import Path

from pyinfra import host
from pyinfra.facts.server import Command
from pyinfra.operations import files, server

REPO = Path(__file__).resolve().parent.parent
# scripts/ holds plain scripts, not a package; pyinfra runs this from tasks/.
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "fleet"))
import mount  # noqa: E402
import render_quadlet  # noqa: E402

# Same module the node runs, so the expected-jar list is written by the code that reads it.
_spec = importlib.util.spec_from_file_location(
    "verify_plugins", REPO / "scripts" / "fleet" / "verify-plugins.py"
)
verify_plugins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_plugins)

IMAGES = json.loads((REPO / "containers" / "images.json").read_text())
REGISTRY = "ghcr.io/jcpowermac/pumpkin-fleet"

# node -> (image, podman --memory limit, JAVA_MEMORY env)
WORKLOADS = {
    "lobby": ("papermc", "4g", "2G"),
    "plugins": ("papermc", "4g", "2G"),
    "vanilla": ("fabric", "3g", "2G"),
    "neoforge": ("neoforge", "6g", "4G"),
}


def _push(name: str, text: str, dest: str, *, sudo: bool) -> None:
    """Paste multi-line text with base64-to-tee — the same way the polkit rule is written."""
    privileged = "sudo " if sudo else ""
    server.shell(
        name=name,
        commands=(
            f"echo {base64.b64encode(text.encode()).decode()} "
            f"| base64 -d | {privileged}tee {dest} >/dev/null"
        ),
    )


node = host.name
image_name, mem, java_mem = WORKLOADS[node]
image = f"{REGISTRY}/{image_name}"
channel = render_quadlet.channel_for(node)
ip = host.data.ssh_hostname
data_dir = f"/srv/{node}/data"
unit = f"~/.config/containers/systemd/{node}.container"

# Rootless podman containers die with the SSH session that started them unless
# the user lingers (logind tears down user@N and its container scopes).
server.shell(name="Enable linger for the service user", commands="loginctl enable-linger core", _sudo=True)
files.directory(path=data_dir, _sudo=True)

# --- the world disk --------------------------------------------------------
# data_dir must be the host-owned zvol (scsi1), never a directory on the disposable OS
# disk: a rebuild re-imports scsi0, and a world written there goes with it. The device is
# resolved from the node's own /dev/disk/by-id entries (the deploy refuses rather than
# guess), and formatting is allowed only on a device that carries no filesystem at all.
data_unit = f"srv-{node}-data.mount"
device = mount.data_device(
    node,
    host.get_fact(Command, command="ls -1 /dev/disk/by-id") or "",
    render_quadlet.pin("FLEET_DATA_DEV"),
)
blkid = host.get_fact(Command, command=f"sudo blkid -o export {device} || true") or ""
# Written before anything else uses it: 25-migrate-data.sh reads the device from this unit.
_push(
    name=f"Install the {node} data mount unit",
    text=mount.mount_unit(node, device),
    dest=f"/etc/systemd/system/{data_unit}",
    sudo=True,
)
# Formatted *before* the guard below can refuse. On a node whose world is still on the OS
# disk the deploy is expected to refuse, and it has to leave the zvol ready for
# 25-migrate-data.sh to mount; with the format below the guard the migration dies in
# `mount` with "wrong fs type, bad option, bad superblock" and never names the cause.
if mount.needs_format(blkid):
    # mkfs.xfs (no -f) refuses a device that already carries a filesystem; the test -b
    # means a missing device fails here instead of becoming an empty world directory.
    server.shell(
        name=f"Format {device} with XFS (first attach only)",
        commands=f"test -b {device} && sudo mkfs.xfs {device}",
        _sudo=True,
    )
elif mount.filesystem_of(blkid) != "xfs":
    # Something already lives on that device. XFS is the fleet's own; anything else is
    # someone's data and a deploy is no reason to reformat it.
    raise SystemExit(
        f"{device} carries {mount.filesystem_of(blkid)}, not xfs — refusing to format over it"
    )
# A non-empty data_dir that is not a mountpoint is a world still on the OS disk (the
# pre-zvol layout). Mounting the unused zvol over it would hide that world and the server
# would start on an empty one, so the deploy stops and names the migration step. This stays
# above the mount op: if a failed guard were ever continued past, the mount would be silent.
server.shell(
    name=f"Check {data_dir} is not an unmigrated world",
    commands=(
        f"mountpoint -q {data_dir} && exit 0; "
        f'if [ -n "$(ls -A {data_dir})" ]; then '
        f"echo '{data_dir} holds data but is not mounted — run scripts/fleet/25-migrate-data.sh {node} first'; "
        "exit 1; fi"
    ),
)
server.shell(
    name=f"Mount {data_dir} from {device}",
    commands=f"systemctl daemon-reload && systemctl enable --now {data_unit}",
    _sudo=True,
)
# The check that makes Review Focus 6 real: an unmounted volume leaves a plain directory on
# the OS disk, and the container would happily write a world into it.
server.shell(
    name=f"{data_dir} is the mounted data volume",
    commands=(
        f"mountpoint -q {data_dir} || "
        f"{{ echo '{data_dir} is not a mountpoint — refusing to write a world onto the OS disk'; "
        f"findmnt -T {data_dir}; exit 1; }}; "
        # The unit file can be rewritten while the active mount stays on the previous device
        # (renamed by-id path, a pin that moved): the deploy would report success and the node
        # would come back on the wrong disk at the next boot.
        f"src=$(findmnt -no SOURCE {data_dir}) || exit 1; "
        f'[ "$src" = "{device}" ] || {{ echo "{data_dir} is mounted from $src, not {device} — "'
        f'"the active mount is not the disk the unit names; a reboot would move the world"; exit 1; }}'
    ),
)

# files.directory above creates it root-owned, and inside `podman unshare` uid 0 IS
# `core`, so a namespace chown of a root-owned dir is EPERM. Hand the directory
# itself to core first (not -R: the contents keep their container ownership).
server.shell(name=f"Hand {data_dir} to the service user", commands=f"chown core:core {data_dir}", _sudo=True)
# Container runs as uid/gid 1000. podman for `core` remaps uids (subuid range),
# so chown inside podman's own namespace — chowning to host uid 1000 does not work.
server.shell(name=f"Own {data_dir} by container uid 1000", commands=f"podman unshare chown -R 1000:1000 {data_dir}")

# --- ops visibility in cockpit (task 7) ----------------------------------
# The container runs under this node's default user (core, uid 1000) rootless podman.
# Operators log into cockpit as an LDAP/authentik user (e.g. jcallen) whose OWN
# podman store is empty. cockpit-podman only lists *other* users' containers when
# the session is privileged (its cgroup scan runs with superuser:"require"). So
# grant the ops authentik group cockpit's root-bridge polkit action. OPS_GROUP is
# the authentik group synced via sssd (docs/ldap-handoff.md).
OPS_GROUP = "fleet-admins"
_polkit_js = (
    'polkit.addRule(function(action, subject) {\n'
    '    if (action.id == "org.cockpit-project.cockpit.root-bridge" &&\n'
    f'        subject.isInGroup("{OPS_GROUP}")) {{\n'
    '            return polkit.Result.YES;\n'
    '        }\n'
    '});\n'
)
server.shell(
    name="Polkit: ops group may run privileged cockpit tasks",
    commands=(
        "echo "
        + base64.b64encode(_polkit_js.encode()).decode()
        + " | sudo base64 -d | sudo tee /etc/polkit-1/rules.d/60-cockpit-ops.rules >/dev/null"
    ),
)
# Cockpit's superuser acquisition prefers the SUDO config (it's first and valid
# whenever `sudo -A` works), and does NOT fall through to the polkit config if
# sudo fails. An LDAP/authentik user not in sudoers therefore gets a non-privileged
# session. Grant the ops group sudo for the bridge — but DELIBERATELY without
# NOPASSWD, so elevation is gated by the operator's own (LDAP) password: Cockpit's
# documented "reuse my password for privileged tasks" model. Consciously opting in
# per session, and auditable in the sudo log. (Needed because the podman panel opens
# other users' podman sockets in the root bridge.)
_sudoers = (
    "# Fleet operators (authentik group via sssd) may run the privileged cockpit\n"
    "# bridge, gated by their own (LDAP) password. No NOPASSWD on purpose.\n"
    f"%{OPS_GROUP} ALL=(root) /usr/sbin/cockpit-bridge, /usr/bin/cockpit-bridge\n"
)
server.shell(
    name="Sudoers: ops group may run privileged cockpit bridge",
    commands=(
        "echo "
        + base64.b64encode(_sudoers.encode()).decode()
        + " | sudo base64 -d | sudo tee /etc/sudoers.d/cockpit-fleet >/dev/null"
        + " && sudo chmod 0440 /etc/sudoers.d/cockpit-fleet"
        + " && sudo visudo -cf /etc/sudoers.d/cockpit-fleet >/dev/null"
    ),
)
# Keep the podman API socket alive across reboots (cockpit-podman connects here;
# with linger on, the user manager stays up so this socket persists).
server.shell(name="Enable podman.socket (user unit)", commands="systemctl --user enable --now podman.socket")

server.shell(
    name=f"Pull {image}:{channel} (skip if already local)",
    commands=f"podman image exists {image}:{channel} || podman pull {image}:{channel}",
)

# Plugins ship inside the image (CI bakes them from containers/images.json), so there is
# nothing to fetch here. The only thing the deploy still needs to know is whether this
# node's image carries OPanel, whose web panel serves on 3000.
plugins = next(i for i in IMAGES if i["name"] == image_name).get("plugins", [])
opanel = next((p for p in plugins if p["name"] == "opanel"), None)

# --- the container's unit -------------------------------------------------
# A user unit, because the server runs rootless as `core`. Quadlet generates the
# service from this file, so it is never `systemctl enable`d: boot starts come from the
# `[Install]` section the generator applies on every daemon-reload (podman-systemd.unit(5)).
server.shell(name="Create the quadlet unit directory", commands="mkdir -p ~/.config/containers/systemd")
_push(
    name=f"Install the {node} quadlet unit",
    text=render_quadlet.render_container_unit(node, image, channel, mem, java_mem, data_dir),
    dest=unit,
    sudo=False,
)
# Hand over any container an earlier `podman run` deploy left under this name: the unit
# refuses to start while the name is taken, and `podman rm -f` would SIGKILL a loaded world.
server.shell(name=f"Hand any pre-quadlet {node} container over to the unit", commands=render_quadlet.handover_command(node))
# The unit's own ConditionPathIsMountPoint is the boot-time guard; this is the one the deploy
# can prove. With the mount inactive the container bind-mounts a plain directory on the OS
# disk and Paper generates a fresh world into it — nothing else would notice until the next
# deploy refused with a message that could not be followed.
server.shell(
    name=f"{data_unit} is active before {node}.service starts",
    commands=(
        f"systemctl is-active --quiet {data_unit} || "
        f"{{ echo '{data_unit} is not active — refusing to start {node}.service: it would "
        f"bind-mount a plain directory on the OS disk and generate a new world'; exit 1; }}"
    ),
    _sudo=True,
)
# `start` on an already-running unit is a no-op, so a redeploy never bounces a live
# server; a new image arrives through podman-auto-update.timer instead.
server.shell(
    name=f"Start {node}.service",
    commands=f"systemctl --user daemon-reload && systemctl --user start {node}.service",
)
# The timer ships at both system and user level; the user one is the one that matches
# rootless quadlets. It compares the local digest with what the channel tag now points
# at, pulls, and restarts the unit.
server.shell(
    name="Enable podman-auto-update.timer (user unit)",
    commands="systemctl --user enable --now podman-auto-update.timer",
)

# --- the plugin check -----------------------------------------------------
# The declared set comes from containers/images.json, never hand-maintained here, so a
# plugin bump changes this file in the same deploy that ships the image.
expected_jars = verify_plugins.expected_jars(REPO / "containers" / "images.json", image_name)
server.shell(name="Create /etc/fleet", commands="mkdir -p /etc/fleet", _sudo=True)
_push(
    name=f"Install the expected plugin list ({image_name})",
    text="".join(f"{name}\n" for name in sorted(expected_jars)),
    dest=f"/etc/fleet/expected-plugins.{image_name}.txt",
    sudo=True,
)
_push(
    name="Install verify-plugins.py",
    text=(REPO / "scripts" / "fleet" / "verify-plugins.py").read_text(),
    dest=f"{render_quadlet.FLEET_LIB}/verify-plugins.py",
    sudo=True,
)
# An expected list for an image this node no longer runs describes a container that does
# not exist; the list is deploy-time state, so a redeploy clears the leftovers.
server.shell(
    name=f"Remove expected plugin lists of other images",
    commands=(
        "find /etc/fleet -maxdepth 1 -name 'expected-plugins.*.txt' "
        f"! -name 'expected-plugins.{image_name}.txt' -delete"
    ),
    _sudo=True,
)
_verify_timer, _verify_service = render_quadlet.render_verify_units(node, image_name, data_dir)
_push(
    name="Install fleet-verify.service",
    text=_verify_service,
    dest=f"{render_quadlet.REGISTRY_UNITS}/fleet-verify.service",
    sudo=True,
)
_push(
    name="Install fleet-verify.timer",
    text=_verify_timer,
    dest=f"{render_quadlet.REGISTRY_UNITS}/fleet-verify.timer",
    sudo=True,
)
# The timer, not just the deploy-time run: podman-auto-update.timer replaces the container
# inside core's user manager and nothing else would re-check it.
server.shell(
    name="Enable fleet-verify.timer",
    commands="systemctl daemon-reload && systemctl enable --now fleet-verify.timer",
    _sudo=True,
)

# uCore runs firewalld (FedoraServer zone) with only ssh/cockpit open; the container
# port bind lands on the host interface and is dropped otherwise. These `/etc` drop-ins
# survive an OSTree rebase — verified on 201 across the uCore -> pumpkin-os rebase
# (spike-notes §16.4).
server.shell(
    name=f"Allow 25565/tcp through firewalld",
    commands="firewall-cmd --add-port=25565/tcp --permanent && firewall-cmd --reload",
    _sudo=True,
)
# OPanel listens on 3000 by default (host networking, so no mapping). It is a
# plugin in the manifest, so the rule follows whichever image declares it — currently
# fabric only. Internal until authentik OIDC + the Traefik route are in front of it.
if opanel:
    server.shell(
        name="Allow 3000/tcp (OPanel) through firewalld",
        commands="firewall-cmd --add-port=3000/tcp --permanent && firewall-cmd --reload",
        _sudo=True,
    )
else:
    # lobby ran the OPanel image before papermc. The permanent rule survived that change and
    # nothing listens on 3000 now, so the deploy closes it; removing an absent port is a no-op.
    server.shell(
        name="Close 3000/tcp (this image has no OPanel) in firewalld",
        commands="firewall-cmd --remove-port=3000/tcp --permanent && firewall-cmd --reload",
        _sudo=True,
    )
# After the firewall rule, not before: the probe hits this node's own host address, which
# firewalld's zone drops on a node where 25565 was never opened.
server.shell(
    name=f"Wait for {node} to open port 25565",
    commands=(
        "for i in $(seq 1 60); do "
        f"timeout 2 bash -c 'exec 3<>/dev/tcp/{ip}/25565' 2>/dev/null && exit 0; "
        "sleep 2; done; "
        f"echo '{node} port 25565 never opened'; podman logs --tail 50 {node}; exit 1"
    ),
)
server.shell(
    name=f"{node} answers on {ip}:25565",
    commands=f"timeout 2 bash -c 'exec 3<>/dev/tcp/{ip}/25565' || exit 1",
)
# Last, because it reads the log of a container that has only just started (the check
# waits for the server's startup-complete line first). The oneshot exits 0 even when it
# reports failures, so a bad plugin set shows up here and in /run/fleet-verify.status —
# never as a failed deploy step.
server.shell(
    name=f"Check the declared plugins loaded on {node}",
    commands="systemctl start fleet-verify.service && cat /run/fleet-verify.status",
    _sudo=True,
)

# --- the reboot window ----------------------------------------------------
# LAST, and the timer is the very last op: `Persistent=true` with no stamp makes a first
# elapse due as soon as the timer exists, so a deploy that installed it earlier could be
# rebooted in the middle of its own firewall/verify steps. Bare-metal reboot, so these are
# *system* units (/etc); the gate reads the node's own server over mc-ping.
_reboot_timer, _reboot_service = render_quadlet.render_reboot_units(
    node, render_quadlet.pin("REBOOT_ONCALENDAR")
)
server.shell(name=f"Create {render_quadlet.FLEET_LIB}", commands=f"mkdir -p {render_quadlet.FLEET_LIB}", _sudo=True)
_push(
    name="Install mc-ping.py (the reboot gate's probe)",
    text=(REPO / "scripts" / "mc-ping.py").read_text(),
    dest=f"{render_quadlet.FLEET_LIB}/mc-ping.py",
    sudo=True,
)
_push(
    name="Install reboot-gate.py",
    text=(REPO / "scripts" / "fleet" / "reboot-gate.py").read_text(),
    dest=f"{render_quadlet.FLEET_LIB}/reboot-gate.py",
    sudo=True,
)
_push(
    name="Install fleet-reboot.service",
    text=_reboot_service,
    dest=f"{render_quadlet.REGISTRY_UNITS}/fleet-reboot.service",
    sudo=True,
)
_push(
    name="Install fleet-reboot.timer",
    text=_reboot_timer,
    dest=f"{render_quadlet.REGISTRY_UNITS}/fleet-reboot.timer",
    sudo=True,
)
# Proved here, before the timer can fire it: a gate whose only other caller is the timer is
# unproven until it has already rebooted something.
server.shell(
    name="Prove the reboot gate on this node (dry run, never reboots)",
    commands=f"/usr/bin/python3 {render_quadlet.FLEET_LIB}/reboot-gate.py --dry-run",
    _sudo=True,
)
server.shell(
    name="Enable fleet-reboot.timer",
    commands="systemctl daemon-reload && systemctl enable --now fleet-reboot.timer",
    _sudo=True,
)
