"""Fleet game containers: pull the pinned image from ghcr.io, run under podman.

Image pins live in containers/images.json (single source of truth). Container
flags follow containers/README.md "Running one". Data dir is bind-mounted at
/data (seeded once by the entrypoint, survives OS rebases).

Usage: uv run pyinfra inventory/hosts.py --limit lobby tasks/gameserver.py
"""
import base64
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
# Keep the podman API socket alive across reboots (cockpit-podman connects here;
# with linger on, the user manager stays up so this socket persists).
server.shell(name="Enable podman.socket (user unit)", commands="systemctl --user enable --now podman.socket")

server.shell(
    name=f"Pull {image} (skip if already local)",
    commands=f"podman image exists {image} || podman pull {image}",
)
server.shell(
    name=f"Run {node} server container",
    commands=(
        f"podman rm -f {node} 2>/dev/null || true; "
        f"podman run -d --name {node} --init "
        f"--restart=unless-stopped "
        f"--memory {mem} --stop-timeout 60 "
        f"--network=host "
        f"-v {data_dir}:/data:Z "
        f"-e JAVA_MEMORY={java_mem} "
        f"{image}"
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
server.shell(
    name=f"{node} answers on {ip}:25565",
    commands=f"timeout 2 bash -c 'exec 3<>/dev/tcp/{ip}/25565' || exit 1",
)
