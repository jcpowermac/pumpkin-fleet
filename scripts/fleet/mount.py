"""The node's world disk: which device, whether it may be formatted, and its mount unit.

Minecraft data lives on a ZFS volume the PVE host owns (`pool/mcdata/<node>`), attached to
the VM as `scsi1`, so it survives a destroy/recreate of the VM whose `scsi0` is thrown away
(docs/superpowers/specs/2026-11-26-fleet-plugin-gitops-design.md §2b). On the node it is a
systemd `.mount` unit rather than an fstab entry: the deploy starts it and can then prove it
is mounted, which is the check that keeps a world out of the disposable OS disk.
"""
import re

BY_ID = "/dev/disk/by-id"
_TYPE = re.compile(r'\bTYPE="?([\w-]+)')
# blkid prints PTTYPE= when the device carries a partition table but no filesystem yet.
_PTTYPE = re.compile(r"\bPTTYPE=")


def data_device(node: str, by_id_listing: str, fallback: str) -> str:
    """The node's data disk: the `/dev/disk/by-id` entry naming this node, else the pin.

    One guessed path cannot be right for four nodes, and "the second disk" is how a world
    gets formatted. Nothing here guesses: with neither a by-id entry nor a pin the deploy
    stops instead of picking a disk.
    """
    name = node.lower()
    # Anchored to a delimiter: an entry that merely embeds the node name
    # (…_HARDDISK_blobby, …_lobby2) is not this node's disk, and silently preferring it
    # over the pin is how the wrong volume gets mounted.
    matches_node = re.compile(rf"(^|[-_.]){re.escape(name)}($|[-_.])")
    for entry in sorted(by_id_listing.split()):
        if matches_node.search(entry.lower()):
            return f"{BY_ID}/{entry}"
    if fallback:
        return fallback
    raise ValueError(
        f"no {BY_ID} entry for {node} (the zvol pool/mcdata/{node}) and FLEET_DATA_DEV is "
        "unset in configs/pins.env — refusing to guess which disk holds the world"
    )


def filesystem_of(blkid_output: str) -> str | None:
    """The filesystem `blkid` reports for the device, or None when it carries none.

    A partition table with no filesystem on the device itself reports PTTYPE but no TYPE;
    that is in use, not blank, and the caller must refuse rather than format it (mkfs.xfs's
    own refusal is version-dependent and is not this fleet's contract).
    """
    text = blkid_output or ""
    match = _TYPE.search(text)
    if match:
        return match.group(1)
    return "partition-table" if _PTTYPE.search(text) else None


def needs_format(blkid_output: str) -> bool:
    """Only a device with no filesystem at all is ever formatted — never one already in use."""
    return filesystem_of(blkid_output) is None


def mount_unit(node: str, device: str) -> str:
    """The `.mount` unit for `/srv/<node>/data`.

    systemd derives the unit's filename from `Where=`, so this must be installed as
    `srv-<node>-data.mount`: under any other name systemd ignores it, the mount never
    happens, and the container writes the world onto the OS disk.
    """
    return f"""\
[Unit]
Description=pumpkin fleet {node}: Minecraft data (host-owned zvol, not the OS disk)

[Mount]
What={device}
Where=/srv/{node}/data
Type=xfs

[Install]
WantedBy=multi-user.target
"""
