"""Render the fleet's quadlet container unit and its reboot window, as text.

Keys are the ones podman-systemd.unit(5) documents, nothing invented. Two splits
matter: the container unit is a *user* unit (the server runs rootless as `core`, so
it cannot be a system unit), while the reboot window is a *system* unit because it
reboots the machine. And a quadlet unit can never be `systemctl enable`d — it is
generated into a generator directory on every daemon-reload — so boot persistence is
the `[Install]` section the generator applies, which is why it is emitted here.
"""

REGISTRY_UNITS = "/etc/systemd/system"
FLEET_LIB = "/usr/local/lib/fleet"


def render_container_unit(
    node: str, image: str, channel: str, memory: str, java_memory: str, data_dir: str
) -> str:
    """`image` is the repository reference without a tag; `channel` is the tag to follow."""
    return f"""\
[Unit]
Description=pumpkin fleet {node} ({image}:{channel})

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
        f"podman stop --time 60 {node} && podman rm {node}"
    )
