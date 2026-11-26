"""tasks/gameserver.py is the only thing that writes a node's verify state.

Three things there go wrong silently in a deploy log: the volume path the check is given
(a jar directory instead of the volume root makes every healthy node report every jar
missing), the timer that keeps the verdict fresh after `podman-auto-update.timer` replaces
the container, and the expected-jar file of an image the node no longer runs. So these
assert on the operations the task actually emits, by executing it against stub pyinfra
modules.
"""
import base64
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import render_quadlet  # noqa: E402

SSH_HOSTNAMES = {
    "plugins": "10.10.10.55",
    "lobby": "10.10.10.54",
    "vanilla": "10.10.10.56",
}
# The node's disks as `/dev/disk/by-id` lists them; the data disk's entry names the node.
BY_ID = {
    node: f"scsi-0QEMU_QEMU_HARDDISK_drive-scsi0\nscsi-0QEMU_QEMU_HARDDISK_{node}\n"
    for node in SSH_HOSTNAMES
}


def deploy_ops(
    node: str, blkid: str = "", pin_dev: str | None = None, by_id: str | None = None
) -> list[tuple[str, dict]]:
    """The operations the task emits for one node, with its facts stubbed.

    `blkid` is what `blkid -o export <device>` reports for the data disk: "" on a device
    that has never been formatted, the real output once it has. `pin_dev` is
    `FLEET_DATA_DEV` as configs/pins.env would carry it, and `by_id` the node's own
    `/dev/disk/by-id` listing (default: one entry naming the node).
    """
    ops: list[tuple[str, dict]] = []
    if pin_dev is not None:
        real_pin = render_quadlet.pin
        render_quadlet.pin = lambda key: pin_dev if key == "FLEET_DATA_DEV" else real_pin(key)

    pyinfra = types.ModuleType("pyinfra")

    class Host:
        name = node
        data = types.SimpleNamespace(ssh_hostname=SSH_HOSTNAMES[node])

        @staticmethod
        def get_fact(fact, **kwargs):
            command = kwargs["command"]
            if command.startswith("ls "):
                return BY_ID[node] if by_id is None else by_id
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
        if pin_dev is not None:
            render_quadlet.pin = real_pin
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


def test_the_fleet_lib_exists_before_anything_is_written_into_it():
    """The reboot window is the last block now, so its `mkdir -p /usr/local/lib/fleet` cannot
    be what creates the directory the plugin check writes verify-plugins.py into: `tee` into a
    missing directory fails a fresh node's first deploy."""
    ops = commands(deploy_ops("plugins"))
    mkdir = next(i for i, c in enumerate(ops) if "mkdir -p /usr/local/lib/fleet" in c)
    pushes = [i for i, c in enumerate(ops) if "/usr/local/lib/fleet" in c and "base64 -d" in c]
    assert pushes, ops
    assert mkdir < min(pushes), ops


def test_the_reboot_timer_is_the_last_thing_the_deploy_does():
    """`Persistent=true` with no stamp makes the first elapse due immediately, so a deploy
    that installed the timer before its firewall/verify steps could be rebooted mid-run."""
    ops = commands(deploy_ops("plugins"))
    enabled = next(i for i, c in enumerate(ops) if "enable --now fleet-reboot.timer" in c)
    assert enabled == len(ops) - 1, ops[enabled + 1 :]


def test_the_deploy_proves_the_gate_before_it_can_fire():
    """A gate that only ever runs from the timer is unproven until it reboots something."""
    ops = commands(deploy_ops("plugins"))
    dry = next(i for i, c in enumerate(ops) if "reboot-gate.py --dry-run" in c)
    enabled = next(i for i, c in enumerate(ops) if "enable --now fleet-reboot.timer" in c)
    assert dry < enabled, ops


def test_the_mounted_device_must_be_the_one_the_unit_names():
    """The unit file can be rewritten while the active mount stays on the old device: the
    deploy would report success and the node would come back on the wrong disk at next boot."""
    check = next(
        c for c in commands(deploy_ops("plugins")) if "findmnt -no SOURCE /srv/plugins/data" in c
    )
    assert f"readlink -f {DATA_DEV}" in check, check


def run_device_check(tmp_path, findmnt_source: str, unit_device_resolves_to: str):
    """Run the emitted findmnt check under bash, with the node's resolution faked.

    `findmnt -no SOURCE` prints what the kernel resolved — a `/dev/zd96`, not the by-id path
    the unit names — so a string comparison between the two fails on every healthy node.
    """
    check = next(
        c for c in commands(deploy_ops("plugins")) if "findmnt -no SOURCE /srv/plugins/data" in c
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "findmnt").write_text('#!/bin/sh\necho "$FINDMNT_SOURCE"\n')
    # `readlink -f <the unit's by-id path>` is the one the stub answers differently.
    (bin_dir / "readlink").write_text(
        '#!/bin/sh\ncase "$2" in */by-id/*) echo "$UNIT_RESOLVES_TO" ;; *) echo "$2" ;; esac\n'
    )
    (bin_dir / "mountpoint").write_text("#!/bin/sh\nexit 0\n")
    for script in bin_dir.iterdir():
        script.chmod(0o755)
    return subprocess.run(
        ["/bin/bash", "-c", check],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FINDMNT_SOURCE": findmnt_source,
            "UNIT_RESOLVES_TO": unit_device_resolves_to,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_a_kernel_resolved_source_is_the_same_device(tmp_path):
    """`/dev/zd96` vs `/dev/disk/by-id/…` is the same disk; comparing the strings would fail
    a healthy node's deploy fleet-wide (loudly, but for the wrong reason)."""
    result = run_device_check(tmp_path, "/dev/zd96", "/dev/zd96")
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_mount_from_another_device_is_still_refused(tmp_path):
    """Resolving both sides must not turn the check into a rubber stamp."""
    refused = run_device_check(tmp_path, "/dev/zd96", "/dev/zd97")
    assert refused.returncode != 0, refused.stdout
    assert "not the disk the unit names" in refused.stdout, refused.stdout


def test_a_node_without_opanel_closes_the_port_its_old_image_opened():
    """lobby runs papermc now; OPanel's 3000/tcp rule survived the image change and nothing
    listens on it, so the deploy takes it back out. vanilla still runs fabric + OPanel."""
    papermc = commands(deploy_ops("plugins"))
    assert any("--remove-port=3000/tcp" in c for c in papermc), papermc
    assert not any("--add-port=3000/tcp" in c for c in papermc), papermc
    # Proved before it is removed, and a zone that never had the port is not a failed deploy.
    close = next(c for c in papermc if "--remove-port=3000/tcp" in c)
    assert close.index("--query-port=3000/tcp") < close.index("--remove-port=3000/tcp"), close
    assert close.endswith("|| true"), close
    fabric = commands(deploy_ops("vanilla"))
    assert any("--add-port=3000/tcp" in c for c in fabric), fabric
    assert not any("--remove-port=3000/tcp" in c for c in fabric), fabric


def test_the_deploy_asserts_the_mount_is_active_before_it_starts_the_container():
    """Fix 4(b): the unit's ConditionPathIsMountPoint is the boot-time guard; this is the
    deploy-time one, and it names the failure instead of letting Paper create a new world."""
    ops = commands(deploy_ops("plugins"))
    asserted = next(i for i, c in enumerate(ops) if "is-active --quiet srv-plugins-data.mount" in c)
    started = next(i for i, c in enumerate(ops) if "--user start plugins.service" in c)
    assert asserted < started, ops
    assert "generate a new world" in ops[asserted]


def test_an_inactive_unit_reports_its_condition_and_the_mount_state_together():
    """`start` on a unit whose Condition* is unmet does not fail — it *skips*, and the deploy
    used to end green over a server that never started. So after the start, an inactive unit
    is a failure that hands the operator the condition and the mount state at once."""
    ops = commands(deploy_ops("plugins"))
    started = next(i for i, c in enumerate(ops) if "systemctl --user start plugins.service" in c)
    diagnosed = next(
        i for i, c in enumerate(ops) if "is-active --quiet plugins.service" in c and "Condition" in c
    )
    assert started < diagnosed, ops
    command = ops[diagnosed]
    assert "srv-plugins-data.mount" in command, command
    assert "mountpoint -q /srv/plugins/data" in command, command
    assert "journalctl --user -u plugins.service" in command, command
    assert command.rstrip().endswith("exit 1"), command


def test_the_pin_is_used_verbatim_when_no_by_id_entry_names_the_node():
    """A host zvol's guest name is slot-derived (`…HARDDISK_drive-scsi1`), which no
    node-name match can recognise: the operator's pin is what the mount unit must name."""
    slot = "scsi-0QEMU_QEMU_HARDDISK_drive-scsi1"
    pinned = deploy_ops(
        "plugins",
        pin_dev=f"/dev/disk/by-id/{slot}",
        by_id=f"scsi-0QEMU_QEMU_HARDDISK_drive-scsi0\n{slot}\n",
    )
    unit = pushed(pinned, "/etc/systemd/system/srv-plugins-data.mount")
    assert f"What=/dev/disk/by-id/{slot}" in unit, unit
    assert f"test -b /dev/disk/by-id/{slot} && sudo mkfs.xfs /dev/disk/by-id/{slot}" in commands(
        pinned
    )


def test_the_deploy_fails_when_the_volume_is_not_mounted():
    checks = [c for c in commands(deploy_ops("plugins")) if "is not a mountpoint" in c]
    assert checks, commands(deploy_ops("plugins"))
    assert f"mountpoint -q /srv/plugins/data" in checks[0]
    assert "exit 1" in checks[0]
