"""tasks/gameserver.py is the only thing that writes a node's verify state.

Three things there go wrong silently in a deploy log: the volume path the check is given
(a jar directory instead of the volume root makes every healthy node report every jar
missing), the timer that keeps the verdict fresh after `podman-auto-update.timer` replaces
the container, and the expected-jar file of an image the node no longer runs. So these
assert on the operations the task actually emits, by executing it against stub pyinfra
modules.
"""
import base64
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SSH_HOSTNAMES = {"plugins": "10.10.10.55", "lobby": "10.10.10.54"}
# The node's disks as `/dev/disk/by-id` lists them; the data disk's entry names the node.
BY_ID = {
    "plugins": "scsi-0QEMU_QEMU_HARDDISK_drive-scsi0\nscsi-0QEMU_QEMU_HARDDISK_plugins\n",
    "lobby": "scsi-0QEMU_QEMU_HARDDISK_drive-scsi0\nscsi-0QEMU_QEMU_HARDDISK_lobby\n",
}


def deploy_ops(node: str, blkid: str = "") -> list[tuple[str, dict]]:
    """The operations the task emits for one node, with its facts stubbed.

    `blkid` is what `blkid -o export <device>` reports for the data disk: "" on a device
    that has never been formatted, the real output once it has.
    """
    ops: list[tuple[str, dict]] = []

    pyinfra = types.ModuleType("pyinfra")

    class Host:
        name = node
        data = types.SimpleNamespace(ssh_hostname=SSH_HOSTNAMES[node])

        @staticmethod
        def get_fact(fact, **kwargs):
            command = kwargs["command"]
            if command.startswith("ls "):
                return BY_ID[node]
            if command.startswith("sudo blkid"):
                return blkid
            raise AssertionError(f"no fact recorded for {command}")

    pyinfra.host = Host()
    facts = types.ModuleType("pyinfra.facts")
    facts_server = types.ModuleType("pyinfra.facts.server")
    facts_server.Command = type("Command", (), {})
    facts.server = facts_server
    pyinfra.facts = facts
    operations = types.ModuleType("pyinfra.operations")
    files = types.ModuleType("pyinfra.operations.files")
    server = types.ModuleType("pyinfra.operations.server")
    files.directory = lambda **kwargs: ops.append(("directory", kwargs))
    server.shell = lambda **kwargs: ops.append(("shell", kwargs))
    operations.files, operations.server = files, server
    injected = {
        "pyinfra": pyinfra,
        "pyinfra.facts": facts,
        "pyinfra.facts.server": facts_server,
        "pyinfra.operations": operations,
        "pyinfra.operations.files": files,
        "pyinfra.operations.server": server,
    }
    saved = {name: sys.modules.get(name) for name in injected}
    sys.modules.update(injected)
    try:
        path = REPO / "tasks" / "gameserver.py"
        exec(compile(path.read_text(), str(path), "exec"), {"__file__": str(path)})
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    return ops


def commands(ops) -> list[str]:
    return [kwargs["commands"] for kind, kwargs in ops if kind == "shell"]


def pushed(ops, fragment: str) -> str:
    """Decode the base64-to-tee payload written to a path containing `fragment`."""
    for command in commands(ops):
        if "base64 -d |" in command and fragment in command:
            payload = command.split("echo ", 1)[1].split(" ", 1)[0]
            return base64.b64decode(payload).decode()
    raise AssertionError(f"nothing was pushed to {fragment}")


def test_the_verify_unit_is_given_the_volume_root_and_the_container_name():
    ops = deploy_ops("plugins")
    unit = pushed(ops, "/etc/systemd/system/fleet-verify.service")
    assert "--data-dir /srv/plugins/data " in unit
    assert "--container plugins" in unit
    assert "/srv/plugins/data/plugins" not in unit


def test_the_deploy_installs_and_enables_the_verify_timer():
    ops = deploy_ops("plugins")
    timer = pushed(ops, "/etc/systemd/system/fleet-verify.timer")
    assert "WantedBy=timers.target" in timer
    assert any(
        "systemctl enable --now fleet-verify.timer" in command for command in commands(ops)
    ), commands(ops)


def test_the_deploy_removes_expected_lists_of_images_this_node_does_not_run():
    """The expected list is deploy-time state: after an image change a leftover
    expected-plugins.<other>.txt describes a container that no longer exists."""
    cleanups = [
        command
        for command in commands(deploy_ops("plugins"))
        if "expected-plugins" in command and " -delete" in command
    ]
    assert cleanups, "the deploy never removes another image's expected list"
    assert "! -name 'expected-plugins.papermc.txt'" in cleanups[0]
    assert "expected-plugins.*.txt" in cleanups[0]


def test_the_expected_list_is_the_manifest_declaration():
    assert pushed(deploy_ops("plugins"), "/etc/fleet/expected-plugins.papermc.txt").split() == [
        "LuckPerms.jar",
        "ViaBackwards.jar",
        "ViaRewind.jar",
        "ViaVersion.jar",
    ]


# --- the world disk (Review Focus 6) ----------------------------------------------
DATA_DEV = "/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_plugins"


def test_the_data_volume_is_a_mount_unit_and_never_an_fstab_line():
    ops = deploy_ops("plugins")
    unit = pushed(ops, "/etc/systemd/system/srv-plugins-data.mount")
    assert f"What={DATA_DEV}" in unit
    assert "Where=/srv/plugins/data" in unit
    assert "Type=xfs" in unit
    assert not any("fstab" in command for command in commands(ops)), commands(ops)
    assert any("systemctl enable --now srv-plugins-data.mount" in c for c in commands(ops))


def test_a_blank_device_is_formatted_and_a_used_one_is_not():
    formatted = [c for c in commands(deploy_ops("plugins")) if "mkfs.xfs" in c]
    assert formatted == [f"test -b {DATA_DEV} && sudo mkfs.xfs {DATA_DEV}"]
    # A world already on the volume is the normal redeploy: nothing touches it.
    reused = [
        command
        for command in commands(deploy_ops("plugins", blkid=f"DEVNAME={DATA_DEV}\nUUID=1234\nTYPE=xfs\n"))
        if "mkfs.xfs" in command
    ]
    assert reused == []


def test_a_device_holding_another_filesystem_is_refused_not_formatted():
    """Formatting over an ext4 device would destroy whatever is on it, so the deploy stops."""
    with pytest.raises(SystemExit) as refused:
        deploy_ops("plugins", blkid=f"DEVNAME={DATA_DEV}\nUUID=1234\nTYPE=ext4\n")
    assert "ext4" in str(refused.value) and "not xfs" in str(refused.value)


def test_a_world_still_on_the_os_disk_stops_the_deploy_before_the_mount():
    """Mounting an unused zvol over a non-empty /srv/<node>/data would hide that world."""
    ops = commands(deploy_ops("plugins"))
    guard = next(i for i, c in enumerate(ops) if "25-migrate-data.sh" in c)
    assert "mountpoint -q /srv/plugins/data" in ops[guard]
    assert ops[guard].index("mountpoint") < ops[guard].index("ls -A")
    unit = next(i for i, c in enumerate(ops) if "srv-plugins-data.mount" in c and "base64 -d" in c)
    mount = next(i for i, c in enumerate(ops) if "systemctl enable --now srv-plugins-data.mount" in c)
    # The unit is installed first (25-migrate-data.sh reads the device out of it) and the
    # guard runs before the mount can hide what it is guarding.
    assert unit < guard < mount


def test_a_blank_device_is_formatted_before_the_unmigrated_world_guard():
    """The only order that protects a world: the zvol is formatted first, so the guard can
    refuse and 25-migrate-data.sh then finds a filesystem to mount. Formatted after the
    guard, the migration dies in `mount` with "wrong fs type, bad option, bad superblock"."""
    ops = commands(deploy_ops("plugins"))
    formatted = next(i for i, c in enumerate(ops) if "mkfs.xfs" in c)
    guard = next(i for i, c in enumerate(ops) if "25-migrate-data.sh" in c)
    assert formatted < guard, ops


def test_a_partition_table_with_no_filesystem_is_refused_not_formatted():
    """`blkid` reports PTTYPE but no TYPE for a partitioned device: in use, not blank."""
    blkid = f"DEVNAME={DATA_DEV}\nPTTYPE=gpt\nPTUUID=1234\n"
    with pytest.raises(SystemExit) as refused:
        deploy_ops("plugins", blkid=blkid)
    assert "partition-table" in str(refused.value) and "not xfs" in str(refused.value)


def test_the_deploy_fails_when_the_volume_is_not_mounted():
    checks = [c for c in commands(deploy_ops("plugins")) if "is not a mountpoint" in c]
    assert checks, commands(deploy_ops("plugins"))
    assert f"mountpoint -q /srv/plugins/data" in checks[0]
    assert "exit 1" in checks[0]
