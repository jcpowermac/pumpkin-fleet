"""Render the fleet's quadlet container unit and its reboot window, as text.

Keys are the ones podman-systemd.unit(5) documents, nothing invented. Two splits
matter: the container unit is a *user* unit (the server runs rootless as `core`, so
it cannot be a system unit), while the reboot window is a *system* unit because it
reboots the machine. And a quadlet unit can never be `systemctl enable`d — it is
generated into a generator directory on every daemon-reload — so boot persistence is
the `[Install]` section the generator applies, which is why it is emitted here.
"""
from pathlib import Path

REGISTRY_UNITS = "/etc/systemd/system"
FLEET_LIB = "/usr/local/lib/fleet"
PINS = Path(__file__).resolve().parent.parent / "configs" / "pins.env"


def pin(key: str) -> str:
    """One value out of configs/pins.env — the file is the only place channels live."""
    for line in PINS.read_text().splitlines():
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.split("#")[0].strip().strip('"')
    raise KeyError(f"{key} is missing from configs/pins.env")


def channel_for(node: str) -> str:
    """The canary is chosen by node name (never a VMID) and follows the pre-promotion tag."""
    return pin("FLEET_CHANNEL_CANARY") if node == pin("CANARY_NODE") else pin("FLEET_CHANNEL")


def render_container_unit(
    node: str, image: str, channel: str, memory: str, java_memory: str, data_dir: str
) -> str:
    """`image` is the repository reference without a tag; `channel` is the tag to follow."""
    # ConditionPathIsMountPoint is emitted in [Unit], which quadlet copies into the unit it
    # generates: a boot with the zvol missing or renamed then leaves the container stopped
    # instead of bind-mounting a plain directory on the OS disk and generating a new world.
    # UNPROVEN UNTIL THE CANARY: podman-systemd.unit(5) documents [Unit] keys as copied, but
    # the generator is not exercised offline, and nothing here can prove a user manager
    # evaluates the key. A failed Condition* *skips* the start with no retry, so the deploy
    # could not see it if it were skipped — hence the deploy-side check after `start`, which
    # reports the condition and the mount state together when the unit comes back inactive.
    # `Before=user@1000.service` on the mount unit (scripts/fleet/mount.py) is what keeps the
    # mount ahead of the user manager at boot, so the condition should never fire at all.
    return f"""\
[Unit]
Description=pumpkin fleet {node} ({image}:{channel})
ConditionPathIsMountPoint={data_dir}

[Container]
ContainerName={node}
Image={image}:{channel}
Network=host
Memory={memory}
Volume={data_dir}:/data:Z
Environment=EULA=true
Environment=JAVA_MEMORY={java_memory}
AutoUpdate=registry
RunInit=true
PodmanArgs=--stop-timeout 60

[Service]
Restart=always
TimeoutStopSec=90

[Install]
WantedBy=default.target
"""


def render_reboot_units(node: str, oncalendar: str) -> tuple[str, str]:
    """Return (timer, service): a randomised window, and the gate that decides."""
    timer = f"""\
[Unit]
Description=pumpkin fleet {node}: maintenance window for the staged OS update

[Timer]
OnCalendar={oncalendar}
RandomizedDelaySec=3600
Persistent=true

[Install]
WantedBy=timers.target
"""
    service = f"""\
[Unit]
Description=pumpkin fleet {node}: reboot if nobody is playing

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {FLEET_LIB}/reboot-gate.py
"""
    return timer, service


def render_verify_units(node: str, image_name: str, data_dir: str) -> tuple[str, str]:
    """Return (timer, service): the node-side plugin check and what re-runs it.

    A *system* oneshot because it writes /run/fleet-verify.status and reads /etc/fleet,
    both root's; `After=<node>.service` is what the brief asks for, but the container is a
    *user* unit, so systemd cannot order across the two managers and the timer is what
    actually schedules the check. Without it `podman-auto-update.timer` could replace the
    container with a bad plugin set and /run/fleet-verify.status would keep the `ok` of the
    last deploy. The server runs rootless under `core`, so the log is read as core with its
    runtime dir — a plain `podman logs` as root looks in root's empty podman store.
    """
    timer = f"""\
[Unit]
Description=pumpkin fleet {node}: re-check that the declared plugins loaded

[Timer]
OnBootSec=60
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
"""
    service = f"""\
[Unit]
Description=pumpkin fleet {node}: did the declared plugins load?
# No effect: the container is a *user* unit and a system unit cannot order after it. The
# timer is what schedules this check; the line is kept only because the brief asks for it.
After={node}.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {FLEET_LIB}/verify-plugins.py --image {image_name} --data-dir {data_dir} --container {node} --log-from "sudo -u core env XDG_RUNTIME_DIR=/run/user/1000 podman logs {node}"
"""
    return timer, service


def handover_command(node: str) -> str:
    """Shell that hands a pre-quadlet `podman run` container over to the unit.

    A container of the same name makes the unit's start fail ("name already in use"),
    and the old deploy's `podman rm -f` SIGKILLs the JVM — a loaded world loses the
    chunks it has not flushed. So stop with the same 60 s grace the unit uses, and
    only when the unit is not already the owner of that container.
    """
    return (
        f"systemctl --user is-active --quiet {node}.service && exit 0; "
        f"podman container exists {node} || exit 0; "
        # `podman stop` fails on a container already in state `exited` (the first-deploy case
        # after a crash or a hand stop), and `&&` would then skip the rm and fail the op.
        # `podman rm` without -f still refuses a running container, so no SIGKILL.
        f"podman stop --time 60 {node} || true; podman rm {node}"
    )
