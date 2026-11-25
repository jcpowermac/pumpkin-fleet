"""Fleet game containers: pull the pinned image from ghcr.io, run under podman.

Image pins live in containers/images.json (single source of truth). Container
flags follow containers/README.md "Running one". Data dir is bind-mounted at
/data (seeded once by the entrypoint, survives OS rebases).

Usage: uv run pyinfra inventory/hosts.py --limit lobby tasks/gameserver.py
"""
import base64
import hashlib
import json
from pathlib import Path

from pyinfra import host
from pyinfra.operations import files, server

REPO = Path(__file__).resolve().parent.parent
IMAGES = json.loads((REPO / "containers" / "images.json").read_text())
REGISTRY = "ghcr.io/jcpowermac/pumpkin-fleet"

# node -> (image, podman --memory limit, JAVA_MEMORY env)
WORKLOADS = {
    "lobby": ("pumpkin", "4g", "2G"),
    "plugins": ("papermc", "4g", "2G"),
    "vanilla": ("pumpkin", "4g", "2G"),
    "neoforge": ("neoforge", "6g", "4G"),
}

node = host.name
image_name, mem, java_mem = WORKLOADS[node]
image = f"{REGISTRY}/{image_name}:{next(i['version'] for i in IMAGES if i['name'] == image_name)}"
ip = host.data.ssh_hostname
data_dir = f"/srv/{node}/data"

# Rootless podman containers die with the SSH session that started them unless
# the user lingers (logind tears down user@N and its container scopes).
server.shell(name="Enable linger for the service user", commands="loginctl enable-linger core", _sudo=True)
files.directory(path=data_dir, _sudo=True)
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
    name=f"Pull {image} (skip if already local)",
    commands=f"podman image exists {image} || podman pull {image}",
)

# OPanel is a native Pumpkin plugin (cdylib) that runs in-process inside the server.
# It goes into the bind-mounted /data volume, not the image: 111 MB, and it is
# version-locked to the pumpkin pin above (mismatch panics the server).
# Written by a throwaway container because a host-side copy lands with the wrong
# SELinux MCS categories and uid mapping, and the server then cannot read it.
# label=disable + no :Z: relabelling a shared volume with :Z makes it private to
# the throwaway container, which then locks the live server out of its own data.
opanel = next(i for i in IMAGES if i["name"] == image_name).get("plugins", {}).get("opanel")
if opanel:
    server.shell(
        name=f"Install OPanel {opanel['version']} into the data volume",
        commands=(
            f"podman run --rm --security-opt label=disable --entrypoint sh -v {data_dir}:/data {image} "
            f"-c 'sha256sum /data/plugins/libopanel_pumpkin.so 2>/dev/null' "
            f"| grep -q {opanel['sha256']} && exit 0; "
            f"curl -fsSL -o /tmp/opanel.so {opanel['url']} && "
            f"echo '{opanel['sha256']}  /tmp/opanel.so' | sha256sum -c - >/dev/null && "
            f"podman run --rm -i --security-opt label=disable --entrypoint sh --user 1000:1000 "
            f"-v {data_dir}:/data {image} "
            f"-c 'cat > /data/plugins/libopanel_pumpkin.so' < /tmp/opanel.so && "
            "rm -f /tmp/opanel.so"
        ),
    )
# Recreating the container wipes OPanel's in-memory session registry, so every
# logged-in browser is 401'd (their JWT is still signed and unexpired, but no
# longer "active"), which shows up as "Unable to connect to the server" plus
# "Unable to connect to the WebSocket of terminal". Upstream /api/auth/check is
# broken (500) so the panel never bounces to login either. So only recreate when
# the deployment inputs actually change; --restart=unless-stopped covers reboots.
_desired = hashlib.sha256(
    f"{image} {mem} {java_mem} {opanel['sha256'] if opanel else ''}".encode()
).hexdigest()
_pin_file = f"~/.fleet-{node}.pin"
server.shell(
    name=f"Run {node} server container",
    commands=(
        f'if [ "$(cat {_pin_file} 2>/dev/null)" = "{_desired}" ] '
        f'&& [ -n "$(podman ps -q --filter name=^{node}$)" ]; then '
        f'echo "{node} already up on this pin, not restarting"; exit 0; fi; '
        f"podman rm -f {node} 2>/dev/null || true; "
        f"podman run -d --name {node} --init "
        f"--restart=unless-stopped "
        f"--memory {mem} --stop-timeout 60 "
        f"--network=host "
        f"-v {data_dir}:/data:Z "
        f"-e JAVA_MEMORY={java_mem} "
        f"{image} "
        f">/dev/null && echo {_desired} > {_pin_file}"
    ),
)
server.shell(
    name=f"Wait for {node} to open port 25565",
    commands=(
        "for i in $(seq 1 60); do "
        f"timeout 2 bash -c 'exec 3<>/dev/tcp/{ip}/25565' 2>/dev/null && exit 0; "
        "sleep 2; done; "
        "echo '{node} port 25565 never opened'; podman logs --tail 50 {node}; exit 1"
    ).format(node=node),
)
# uCore runs firewalld (FedoraServer zone) with only ssh/cockpit open; the
# container port bind lands on the host interface and is dropped otherwise.
# (Does not survive an OSTree rebase — revisit for Task 7 Step 3.)
server.shell(
    name=f"Allow 25565/tcp through firewalld",
    commands="firewall-cmd --add-port=25565/tcp --permanent && firewall-cmd --reload",
    _sudo=True,
)
# OPanel listens on 3000 by default (host networking, so no mapping). Internal
# only until Task 7 Step 5 puts authentik OIDC + the Traefik route in front of it.
if opanel:
    server.shell(
        name="Allow 3000/tcp (OPanel) through firewalld",
        commands="firewall-cmd --add-port=3000/tcp --permanent && firewall-cmd --reload",
        _sudo=True,
    )
server.shell(
    name=f"{node} answers on {ip}:25565",
    commands=f"timeout 2 bash -c 'exec 3<>/dev/tcp/{ip}/25565' || exit 1",
)
