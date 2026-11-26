"""The pve3 plumbing: a pinned FCOS build, and a data disk that cannot lose a world.

Three scripts decide where a world lives. `10-download-fcos.sh` names the FCOS build a
rebuild imports (pinned, not discovered). `15-ensure-data-disks.sh` creates the host-owned
zvol `pool/mcdata/<node>` and attaches it as `scsi1`, so `qm destroy --purge` cannot take
the world with the OS disk. `25-migrate-data.sh` moves an older `/srv/<node>/data` onto
that zvol and never deletes the old tree.

Every test runs against a simulated host: a JSON state file behind a stub `ssh` for pve3,
a state file behind stub `mount`/`umount`/`mountpoint`/`systemctl` for the node, and a stub
`curl` that serves recorded fixtures. Nothing here reaches 172.31.12.99 or the network.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(REPO / "scripts" / "fleet"))
import mount  # noqa: E402

NODES = [("201", "lobby"), ("202", "plugins"), ("203", "vanilla"), ("204", "neoforge")]
BY_ID = "/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_lobby"


def _stub(bin_dir: Path, name: str, text: str) -> Path:
    path = bin_dir / name
    path.write_text(text)
    path.chmod(0o755)
    return path


def _env(**extra: str) -> dict:
    env = dict(os.environ)
    env.pop("FLEET_SRV_ROOT", None)
    env.pop("FLEET_UNIT_DIR", None)
    env.update(extra)
    return env


# --- pve3, as a JSON state file -----------------------------------------------------------
PVE_STUB = '''#!/usr/bin/env python3
"""Stand-in for `ssh root@172.31.12.99`: a state file, never a host."""
import json
import os
import sys

argv = sys.argv[1:]
with open(os.environ["PVE_CALLS"], "a") as log:
    log.write(" ".join(argv) + "\\n")
state = json.loads(open(os.environ["PVE_STATE"]).read())
command = argv[1:]
datasets, vms = state["datasets"], state["vms"]


def save():
    open(os.environ["PVE_STATE"], "w").write(json.dumps(state))


if command[:2] == ["zfs", "list"]:
    if command[-1] not in datasets:
        sys.exit(1)
    print(command[-1])
elif command[:2] == ["zfs", "create"]:
    # `zfs create` does not create a missing parent unless it is told to (-p), and a fresh
    # host has no pool/mcdata at all. Modelled here so a -p-less script fails in the test.
    flags = [arg for arg in command[2:] if arg.startswith("-")]
    name = [arg for arg in command[2:] if not arg.startswith("-")][-1]
    parent = name.rsplit("/", 1)[0]
    if name in datasets:
        sys.stderr.write(f"cannot create '{name}': dataset already exists\\n")
        sys.exit(1)
    if parent not in datasets and "-p" not in flags and parent != "pool":
        sys.stderr.write(f"cannot create '{name}': parent '{parent}' does not exist\\n")
        sys.exit(1)
    if parent not in datasets:
        datasets.append(parent)
    datasets.append(name)
    save()
elif command[:2] == ["qm", "status"]:
    if command[2] not in vms:
        sys.stderr.write(f"Configuration file 'nodes/{command[2]}.conf' does not exist\\n")
        sys.exit(1)
    print("status: running")
elif command[:2] == ["qm", "config"]:
    if command[2] not in vms:
        sys.stderr.write(f"Configuration file 'nodes/{command[2]}.conf' does not exist\\n")
        sys.exit(1)
    for key, value in vms[command[2]].items():
        print(f"{key}: {value}")
elif command[:2] == ["qm", "set"]:
    vmid, key, value = command[2], command[3].lstrip("-"), command[4]
    if key in vms.get(vmid, {}):
        sys.stderr.write(f"{key} is already set on {vmid}\\n")
        sys.exit(1)
    vms[vmid][key] = value
    save()
elif command[:1] == ["mkdir"]:
    pass                      # `ssh pve3 mkdir -p /var/coreos/snippets`
elif command[:1] == ["pct"]:
    print("ldap-bind-user ldap-bind-authtok")   # the sssd bind credential out of CT 150
elif command[:1] == ["bash"]:
    # `ssh pve3 bash -s <<EOF` — the per-node create/rebuild script. Recorded, not executed:
    # what a test can see is whether the run got as far as the destroy at all.
    body = sys.stdin.read()
    with open(os.environ["PVE_CALLS"], "a") as log:
        log.write("<<<heredoc\\n" + body + ">>>heredoc\\n")
else:
    sys.stderr.write(f"unsupported on this simulated host: {' '.join(command)}\\n")
    sys.exit(1)
'''


class Pve:
    def __init__(self, root: Path):
        self.bin = root / "bin"
        self.bin.mkdir()
        self.state = root / "pve-state.json"
        self.calls = root / "pve-calls.log"
        _stub(self.bin, "ssh", PVE_STUB)
        # The rebuild pushes one Ignition per node; a real scp here would reach 172.31.12.99.
        _stub(self.bin, "scp", '#!/usr/bin/env bash\necho "scp $*" >> "$PVE_CALLS"\n')
        self.write_state(
            {
                "datasets": [],
                "vms": {vmid: {"scsi0": f"pool:vm-{vmid}-disk-0"} for vmid, _ in NODES},
            }
        )
        self.calls.write_text("")

    def write_state(self, state: dict) -> None:
        self.state.write_text(json.dumps(state))

    def read_state(self) -> dict:
        return json.loads(self.state.read_text())

    def call_log(self) -> str:
        return self.calls.read_text()

    def env(self) -> dict:
        return _env(
            PVE_STATE=str(self.state),
            PVE_CALLS=str(self.calls),
            PATH=f"{self.bin}:{os.environ['PATH']}",
        )


@pytest.fixture
def pve(tmp_path):
    return Pve(tmp_path)


def ensure_data_disks(pve: Pve, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(REPO / "scripts" / "fleet" / "15-ensure-data-disks.sh"), *args],
        capture_output=True,
        text=True,
        env=pve.env(),
    )


def test_data_disk_attach_is_a_host_zvol(pve):
    """scsi1 is a path PVE did not create, which is exactly why --purge leaves it alone."""
    result = ensure_data_disks(pve, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "--scsi1 /dev/zvol/pool/mcdata/lobby" in result.stdout
    assert "pool:vm-201-disk-1" not in result.stdout
    for node in ("lobby", "plugins", "vanilla", "neoforge"):
        # -p: `zfs create` does not create a missing parent, and a fresh host has no
        # pool/mcdata. Without it the first real run aborts under set -e.
        assert f"zfs create -p -V 40G pool/mcdata/{node}" in result.stdout


def test_dry_run_is_identical_twice_and_creates_nothing(pve):
    first = ensure_data_disks(pve, "--dry-run")
    second = ensure_data_disks(pve, "--dry-run")
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    assert pve.read_state()["datasets"] == []
    assert not any("scsi1" in vm for vm in pve.read_state()["vms"].values())
    log = pve.call_log()
    assert "zfs create" not in log and "qm set" not in log, log


def test_a_real_run_twice_is_a_noop_the_second_time(pve):
    first = ensure_data_disks(pve)
    assert first.returncode == 0, first.stderr
    state = pve.read_state()
    # The parent is created once, by the first zvol, and never again.
    assert state["datasets"] == ["pool/mcdata"] + [f"pool/mcdata/{node}" for _, node in NODES]
    assert state["vms"]["201"]["scsi1"] == "/dev/zvol/pool/mcdata/lobby"
    assert state["vms"]["204"]["scsi1"] == "/dev/zvol/pool/mcdata/neoforge"

    marker = len(pve.call_log())
    second = ensure_data_disks(pve)
    assert second.returncode == 0, second.stderr
    applied = pve.call_log()[marker:]
    assert "zfs create" not in applied and "qm set" not in applied, applied
    assert pve.read_state() == state


def test_an_existing_zvol_with_a_world_on_it_is_left_alone(pve):
    """The rebuild case: the VM is gone, the world is not, and it is reattached as it was."""
    state = pve.read_state()
    state["datasets"] = ["pool/mcdata", "pool/mcdata/plugins"]
    pve.write_state(state)
    result = ensure_data_disks(pve, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "zfs create -p -V 40G pool/mcdata/plugins" not in result.stdout
    assert "--scsi1 /dev/zvol/pool/mcdata/plugins" in result.stdout


def test_scsi1_backed_by_something_else_is_refused(pve):
    """Detaching a disk that already holds something is not a rebuild step; it is a decision."""
    state = pve.read_state()
    state["vms"]["203"]["scsi1"] = "pool:vm-203-disk-1"
    pve.write_state(state)

    result = ensure_data_disks(pve)
    assert result.returncode != 0
    assert "REFUSE" in result.stderr and "203" in result.stderr, result.stderr
    assert "pool:vm-203-disk-1" in result.stderr
    # Decided first, executed second: an unsafe node stops the run before anything is touched.
    log = pve.call_log()
    assert "zfs create" not in log and "qm set" not in log, log
    assert pve.read_state()["vms"]["203"]["scsi1"] == "pool:vm-203-disk-1"


# --- the world disk on the node -----------------------------------------------------------
def test_mount_unit_paths():
    unit = mount.mount_unit("lobby", BY_ID)
    assert "What=" + BY_ID in unit
    assert "Where=/srv/lobby/data" in unit
    assert "Type=xfs" in unit
    assert "fstab" not in unit


def test_blank_device_formats_and_used_device_does_not():
    assert mount.needs_format("") is True
    assert mount.needs_format("DEVNAME=/dev/zd96\n") is True
    assert mount.needs_format("DEVNAME=/dev/zd96\nUUID=1234\nTYPE=xfs\n") is False
    assert mount.needs_format('DEVNAME=/dev/zd96: UUID="1234" TYPE="xfs"') is False
    # A filesystem that is not xfs is a world (or someone's disk): never a format candidate.
    assert mount.needs_format("DEVNAME=/dev/zd96\nTYPE=ext4\n") is False
    assert mount.filesystem_of("DEVNAME=/dev/zd96\nTYPE=ext4\n") == "ext4"
    # A partition table with no filesystem on the device is in use too: `blkid` reports no
    # TYPE, and mkfs.xfs's own refusal is version-dependent, not this fleet's contract.
    assert mount.needs_format("DEVNAME=/dev/zd96\nPTTYPE=gpt\nPTUUID=1234\n") is False
    assert mount.filesystem_of("DEVNAME=/dev/zd96\nPTTYPE=gpt\n") == "partition-table"
    assert mount.filesystem_of("") is None


def test_the_device_is_never_guessed():
    listing = "scsi-0QEMU_QEMU_HARDDISK_drive-scsi0\nscsi-0QEMU_QEMU_HARDDISK_lobby\nwwn-0x5000"
    assert mount.data_device("lobby", listing, "/dev/pin") == BY_ID
    # No entry names the node: fall back to the pin, not to the first disk in the listing.
    assert mount.data_device("lobby", "scsi-0QEMU_QEMU_HARDDISK_drive-scsi1\n", "/dev/pin") == "/dev/pin"
    # A by-id name that merely embeds the node name is not this node's disk: the match is
    # anchored to a delimiter, so the pin wins instead.
    assert mount.data_device("lobby", "scsi-0QEMU_QEMU_HARDDISK_blobby\n", "/dev/pin") == "/dev/pin"
    assert mount.data_device("lobby", "scsi-0QEMU_QEMU_HARDDISK_lobby2\n", "/dev/pin") == "/dev/pin"
    with pytest.raises(ValueError) as refused:
        mount.data_device("lobby", "scsi-0QEMU_QEMU_HARDDISK_drive-scsi1\n", "")
    assert "FLEET_DATA_DEV" in str(refused.value)
    assert "lobby" in str(refused.value)


# --- the migration onto the zvol ----------------------------------------------------------
NODE_STUB = '''#!/usr/bin/env python3
"""Node-side tools, faked: `mount`, `umount`, `mountpoint`, `systemctl`.

The state file holds one "<device> <dir>" line per active mount, so a script that mounts and
then asks `mountpoint` gets the answer a real node would give. Every call is logged, so a
test can assert what did and did not run.
"""
import os
import sys

tool = os.path.basename(sys.argv[0])
args = sys.argv[1:]
with open(os.environ["NODE_CALLS"], "a") as log:
    log.write(" ".join([tool, *args]) + "\\n")
state_path = os.environ["NODE_MOUNTS"]
mounts = []
if os.path.exists(state_path):
    mounts = [line.split(" ", 1) for line in open(state_path).read().splitlines()]


def mounted(directory):
    return any(where == directory for _, where in mounts)


def save():
    open(state_path, "w").write("".join(f"{device} {where}\\n" for device, where in mounts))


if tool == "mount":
    if os.environ.get("NODE_MOUNT_FAIL"):
        # e.g. "wrong fs type, bad option, bad superblock": the device carries no filesystem
        # because the deploy that was supposed to format it never got there.
        sys.stderr.write(f"mount: {args[0]}: wrong fs type, bad option, bad superblock\\n")
        sys.exit(32)
    if mounted(args[1]):
        sys.exit(1)
    mounts.append([args[0], args[1]])
    save()
elif tool == "umount":
    if not mounted(args[0]):
        sys.exit(1)
    mounts[:] = [pair for pair in mounts if pair[1] != args[0]]
    save()
elif tool == "mountpoint":
    directory = [arg for arg in args if not arg.startswith("-")][0]
    if not mounted(directory):
        if "-q" not in args:
            print(f"{directory} is not a mountpoint")
        sys.exit(1)
elif tool == "systemctl":
    if "is-active" in args:
        # 0 active, 3 inactive/unknown — the two answers a real systemctl gives a unit.
        sys.exit(0 if os.environ.get("NODE_UNIT_ACTIVE") == "1" else 3)
    if "daemon-reload" in args:
        sys.exit(0)
    if os.environ.get("NODE_SYSTEMCTL_FAIL"):
        print(f"Job for {args[-1]} failed", file=sys.stderr)
        sys.exit(1)
    unit = args[-1]
    text = open(os.path.join(os.environ["FLEET_UNIT_DIR"], unit)).read()
    fields = dict(
        line.split("=", 1) for line in text.splitlines() if "=" in line
    )
    node = unit.split("-")[1]
    where = os.path.join(os.environ["FLEET_SRV_ROOT"], node, "data")
    mounts.append([fields["What"], where])
    save()
else:
    sys.stderr.write(f"unsupported tool: {tool}\\n")
    sys.exit(1)
'''


class Node:
    def __init__(self, root: Path):
        self.root = root
        self.srv = root / "srv"
        self.data = self.srv / "lobby" / "data"
        (self.data / "world").mkdir(parents=True)
        (self.data / "world" / "level.dat").write_text("a world")
        (self.data / "server.properties").write_text("motd=pumpkin\n")
        self.etc = root / "etc"
        self.etc.mkdir()
        (self.etc / "srv-lobby-data.mount").write_text(
            f"[Mount]\nWhat={BY_ID}\nWhere=/srv/lobby/data\nType=xfs\n"
        )
        self.bin = root / "bin"
        self.bin.mkdir()
        self.stub = _stub(self.bin, "hoststub", NODE_STUB)
        for tool in ("mount", "umount", "mountpoint", "systemctl"):
            (self.bin / tool).symlink_to(self.stub)
        # `sudo -u core env XDG_RUNTIME_DIR=... systemctl --user …` is how the script asks
        # core's own user manager about the container unit; drop sudo's own arguments and
        # run the rest, so the stub systemctl answers it.
        sudo = _stub(
            self.bin,
            "sudo",
            '#!/usr/bin/env python3\n'
            "import os, sys\n"
            "args = sys.argv[1:]\n"
            'if args[:1] == ["-u"]:\n'
            "    args = args[2:]\n"
            "os.execvp(args[0], args)\n",
        )
        sudo.chmod(0o755)
        self.tmp = self.srv / "lobby" / "data.tmp"
        self.aside = self.srv / "lobby" / "data.pre-zvol"
        self.mounts = root / "node-mounts"
        self.calls = root / "node-calls.log"
        self.calls.write_text("")

    def env(self) -> dict:
        return _env(
            FLEET_SRV_ROOT=str(self.srv),
            FLEET_UNIT_DIR=str(self.etc),
            NODE_MOUNTS=str(self.mounts),
            NODE_CALLS=str(self.calls),
            PATH=f"{self.bin}:{os.environ['PATH']}",
        )

    def call_log(self) -> str:
        return self.calls.read_text()

    def active_mounts(self) -> list[str]:
        if not self.mounts.exists():
            return []
        return self.mounts.read_text().splitlines()


@pytest.fixture
def node(tmp_path):
    return Node(tmp_path)


def migrate(
    node: Node, *args: str, env: dict | None = None, name: str = "lobby"
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(REPO / "scripts" / "fleet" / "25-migrate-data.sh"), *args, name],
        capture_output=True,
        text=True,
        env=env or node.env(),
    )


def test_migration_dry_run_shows_the_commands_and_does_nothing(node):
    result = migrate(node, "--dry-run")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert f"mount {BY_ID}" in out
    assert f"cp -a {node.data}/. " in out
    # Byte-for-byte, not an entry count: equal counts do not mean a faithful copy.
    assert f"diff -r --no-dereference {node.data} {node.tmp}" in out
    assert f"mv {node.data} {node.aside}" in out
    assert "systemctl start srv-lobby-data.mount" in out
    # The preview reads state (that is how it can refuse) and changes nothing.
    assert node.data.joinpath("world", "level.dat").exists()
    assert not (node.srv / "lobby" / "data.pre-zvol").exists()
    assert all(
        line.startswith("mountpoint ") or "is-active" in line
        for line in node.call_log().splitlines()
    ), node.call_log()


def test_migration_moves_the_old_tree_and_never_deletes_it(node):
    result = migrate(node)
    assert result.returncode == 0, result.stderr
    # The copy landed in the (faked) mounted volume and `diff -r` compared it before the move.
    assert (node.tmp / "world" / "level.dat").read_text() == "a world"
    assert (node.tmp / "server.properties").exists()
    # The old tree was moved aside, not consumed: every file is still there.
    assert (node.aside / "world" / "level.dat").exists()
    assert (node.aside / "server.properties").exists()
    assert node.data.is_dir() and not any(node.data.iterdir())
    assert any("lobby/data" in line and BY_ID in line for line in node.active_mounts())
    assert " rm " not in node.call_log() and "rm " not in node.call_log()


def test_migration_refuses_an_empty_data_dir(node):
    import shutil

    for path in node.data.iterdir():
        shutil.rmtree(path) if path.is_dir() else path.unlink()
    result = migrate(node)
    assert result.returncode != 0
    assert "REFUSE" in result.stderr and "empty" in result.stderr, result.stderr
    assert node.call_log().strip() == f"mountpoint -q {node.data}"


def test_migration_refuses_a_zvol_that_already_holds_data(node):
    (node.srv / "lobby" / "data.tmp").mkdir()
    (node.srv / "lobby" / "data.tmp" / "someone-elses.world").write_text("not mine\n")
    result = migrate(node)
    assert result.returncode != 0
    assert "REFUSE" in result.stderr and "already holds data" in result.stderr, result.stderr
    # The operator has to know the volume may hold half a copy, and that the old tree is
    # still the world: deleting the zvol's contents is then an informed decision.
    assert "partial copy from an aborted" in result.stderr, result.stderr
    assert f"{node.data} is untouched" in result.stderr, result.stderr
    # Nothing was copied and nothing was moved; the old tree is where it was.
    assert (node.data / "world" / "level.dat").exists()
    assert not (node.srv / "lobby" / "data.pre-zvol").exists()


def test_migration_refuses_while_the_container_unit_is_still_running(node):
    """A live server makes the whole sequence a lie: `cp -a` takes a snapshot of a world that
    is still being written, `diff -r` then passes against that snapshot, and the `mv` moves
    the directory out from under the live bind mount — the next deploy starts on the older
    zvol copy and nobody is told."""
    result = migrate(node, env=node.env() | {"NODE_UNIT_ACTIVE": "1"})
    assert result.returncode != 0
    assert "REFUSE" in result.stderr and "lobby.service is running" in result.stderr, result.stderr
    assert f"systemctl --user stop lobby.service" in result.stderr, result.stderr
    # Read-only probe only: nothing was mounted, copied or moved.
    assert node.call_log().splitlines() == [
        f"mountpoint -q {node.data}",
        "systemctl --user is-active --quiet lobby.service",
    ], node.call_log()
    assert not node.tmp.exists() and not node.aside.exists()
    assert (node.data / "world" / "level.dat").exists()


def test_migration_reloads_systemd_before_it_starts_the_mount_unit(node):
    """The unit has never been read by systemd (the deploy wrote the file and then refused),
    so `systemctl start` without a reload fails with `Unit ... not found` — after the tree
    has already moved, and the failure message then blames the mount."""
    result = migrate(node)
    assert result.returncode == 0, result.stderr
    calls = node.call_log().splitlines()
    reloads = [i for i, line in enumerate(calls) if line == "systemctl daemon-reload"]
    starts = [i for i, line in enumerate(calls) if "systemctl start srv-lobby-data.mount" in line]
    assert reloads and starts, calls
    assert reloads[-1] == starts[-1] - 1, calls


def test_migration_names_a_device_that_does_not_mount(node):
    """`set -e` used to kill the script at `mount` with an empty exit status; the refusal it
    was written to print never ran. The device is unformatted exactly when the deploy never
    got to its format step."""
    result = migrate(node, env=node.env() | {"NODE_MOUNT_FAIL": "1"})
    assert result.returncode != 0
    assert "REFUSE" in result.stderr and "did not mount" in result.stderr, result.stderr
    assert str(node.data) in result.stderr
    assert not node.aside.exists()
    assert (node.data / "world" / "level.dat").exists()


def test_migration_unmounts_and_refuses_when_the_copy_is_not_faithful(node):
    """A copy that lost a file must leave the zvol unmounted, not mounted and half full."""
    flaky = node.bin / "cp"
    flaky.write_text(
        "#!/usr/bin/env bash\n"
        "# simulates a copy that silently lost a file\n"
        "n=$#; dst=\"${!n}\"; n=$((n - 1)); src=\"${!n}\"\n"
        'exec /bin/cp "$src/server.properties" "$dst/"\n'
    )
    flaky.chmod(0o755)
    result = migrate(node)
    assert result.returncode != 0
    assert "REFUSE" in result.stderr and "differ" in result.stderr, result.stderr
    # Unmounted on the way out: the next run must be able to try again.
    assert node.active_mounts() == []
    assert "umount " in node.call_log()
    # Nothing was moved and the old tree is untouched.
    assert (node.data / "world" / "level.dat").exists()
    assert not node.aside.exists()


def test_migration_names_the_old_tree_when_the_mount_fails(node):
    """If `systemctl start` fails after the move, the operator must be told where the world is."""
    result = migrate(node, env=node.env() | {"NODE_SYSTEMCTL_FAIL": "1"})
    assert result.returncode != 0
    assert str(node.aside) in result.stderr, result.stderr
    assert (node.aside / "world" / "level.dat").exists()
    assert node.data.is_dir() and not any(node.data.iterdir())


def test_migration_rejects_a_node_name_that_is_not_a_fleet_node(node):
    result = migrate(node, name="../lobby")
    assert result.returncode == 2
    assert "lobby plugins vanilla neoforge" in result.stderr, result.stderr
    assert node.call_log() == ""


def test_migration_refuses_when_a_previous_aside_is_still_there(node):
    """A second migration must not overwrite the first one's evidence."""
    (node.srv / "lobby" / "data.pre-zvol").mkdir()
    result = migrate(node)
    assert result.returncode != 0
    assert "already exists" in result.stderr, result.stderr
    assert node.call_log().strip() == f"mountpoint -q {node.data}"


def test_migration_refuses_when_the_data_dir_is_already_a_mountpoint(node):
    node.mounts.write_text(f"{BY_ID} {node.data}\n")
    result = migrate(node)
    assert result.returncode != 0
    assert "already a mountpoint" in result.stderr, result.stderr
    assert node.call_log().strip() == f"mountpoint -q {node.data}"


# --- the FCOS import -----------------------------------------------------------------------
CURL_STUB = '''#!/usr/bin/env python3
"""Serves the recorded FCOS metadata; refuses anything else, so a stray image download fails."""
import os
import sys

url = sys.argv[-1]
with open(os.environ["CURL_CALLS"], "a") as log:
    log.write(url + "\\n")
if url.endswith("/meta.json"):
    fixture = "fcos-meta-45.20261030.3.1.json"
elif url.endswith("/streams/stable.json"):
    fixture = "fcos-streams-stable.json"
else:
    sys.stderr.write(f"the tests have no recorded response for {url}\\n")
    sys.exit(22)
print(open(os.path.join(os.environ["FIXTURES"], fixture)).read(), end="")
'''


class Laptop:
    """A copy of the tree the script runs in, so nothing here writes the repo's pins.env."""

    def __init__(self, tmp_path: Path, pins: dict | None = None):
        self.root = tmp_path / "repo"
        (self.root / "scripts" / "spike").mkdir(parents=True)
        (self.root / "configs").mkdir()
        (self.root / "build").mkdir()
        pins_text = (REPO / "configs" / "pins.env").read_text()
        for key, value in (pins or {}).items():
            pins_text = "\n".join(
                f"{key}={value}" if line.startswith(f"{key}=") else line
                for line in pins_text.splitlines()
            )
        self.pins = self.root / "configs" / "pins.env"
        self.pins.write_text(pins_text + "\n")
        script = self.root / "scripts" / "spike" / "10-download-fcos.sh"
        script.write_text((REPO / script.relative_to(self.root)).read_text())
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        _stub(self.bin, "curl", CURL_STUB)
        self.curl_calls = tmp_path / "curl-calls.log"
        self.curl_calls.write_text("")
        self.ssh_calls = tmp_path / "ssh-calls.log"
        self.ssh_calls.write_text("")
        _stub(self.bin, "ssh", '#!/usr/bin/env bash\necho "$*" >> "$SSH_CALLS"\necho "FCOS_BUILD=45.20261030.3.1"\n')

    def env(self) -> dict:
        return _env(
            FIXTURES=str(FIXTURES),
            CURL_CALLS=str(self.curl_calls),
            SSH_CALLS=str(self.ssh_calls),
            PATH=f"{self.bin}:{os.environ['PATH']}",
        )

    def download(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(self.root / "scripts" / "spike" / "10-download-fcos.sh"), *args],
            capture_output=True,
            text=True,
            env=self.env(),
        )


def test_pinned_fcos_build_is_used_verbatim(tmp_path):
    laptop = Laptop(tmp_path, {"FCOS_BUILD": "45.20261030.3.1"})
    before = laptop.pins.read_text()
    result = laptop.download("--print-url")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "45.20261030.3.1" in out
    assert (
        "https://builds.coreos.fedoraproject.org/prod/streams/stable/builds/"
        "45.20261030.3.1/x86_64/meta.json" in out
    )
    assert (
        "fedora-coreos-45.20261030.3.1-proxmoxve.x86_64.qcow2.xz" in out
    ), "the proxmoxve artifact, not the qemu one"
    assert "afb24063a5464ce6b7edd7ac84440b408e40699886fbff9f01d2b8582a9f2dba" in out
    # A preview: no image download, no pve3 contact, and pins.env still says the same thing.
    assert laptop.ssh_calls.read_text() == ""
    assert laptop.pins.read_text() == before


def test_auto_resolves_live_stream(tmp_path):
    laptop = Laptop(tmp_path, {"FCOS_BUILD": "auto"})
    before = laptop.pins.read_text()
    result = laptop.download("--print-url")
    assert result.returncode == 0, result.stderr
    assert "raw.githubusercontent.com/coreos/fedora-coreos-streams" in result.stdout
    assert "45.20261030.3.1" in result.stdout
    assert laptop.pins.read_text() == before


def test_auto_records_the_build_it_resolved(tmp_path):
    """auto is the one case that moves the pin: it is the only case where nothing pins it."""
    laptop = Laptop(tmp_path, {"FCOS_BUILD": "auto"})
    result = laptop.download()
    assert result.returncode == 0, result.stderr
    assert "FCOS_BUILD=45.20261030.3.1" in laptop.pins.read_text().splitlines()


def test_the_pinned_fetch_imports_that_build_and_leaves_the_pin_alone(tmp_path):
    laptop = Laptop(tmp_path, {"FCOS_BUILD": "45.20261030.3.1"})
    before = laptop.pins.read_text()
    result = laptop.download()
    assert result.returncode == 0, result.stderr
    remote = (laptop.root / "build" / "pve3-download.sh").read_text()
    assert "fedora-coreos-45.20261030.3.1-proxmoxve.x86_64.qcow2.xz" in remote
    assert "afb24063a5464ce6b7edd7ac84440b408e40699886fbff9f01d2b8582a9f2dba" in remote
    assert "/tmp/ucore-spike-fcos-45.20261030.3.1.qcow2" in remote
    assert laptop.ssh_calls.read_text().strip(), "the fetch must still reach pve3"
    assert laptop.pins.read_text() == before


# --- the rebuild path ----------------------------------------------------------------------
def test_create_nodes_names_the_missing_sssd_credential(tmp_path):
    """A rebuild must stop with a named error, not boot four nodes with an empty bind dn."""
    work = tmp_path
    home = work / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519.pub").write_text("ssh-ed25519 AAAAnotaprivatekey test@host\n")
    bin_dir = work / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "ssh", "#!/usr/bin/env bash\nexit 255\n")
    env = _env(HOME=str(home), PATH=f"{bin_dir}:{os.environ['PATH']}")
    result = subprocess.run(
        ["bash", str(REPO / "scripts" / "fleet" / "20-create-nodes.sh")],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode != 0
    assert "authentik LXC 150 unreadable — sssd bind credential unavailable" in result.stderr
    assert result.stdout == "", "nothing may be created or pushed before the credential is read"


def create_nodes(pve: Pve, root: Path) -> subprocess.CompletedProcess:
    """Run 20-create-nodes.sh against the simulated host, with a public key and no private."""
    home = root / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519.pub").write_text("ssh-ed25519 AAAAnotaprivatekey fleet@test\n")
    return subprocess.run(
        ["bash", str(REPO / "scripts" / "fleet" / "20-create-nodes.sh")],
        capture_output=True,
        text=True,
        env=pve.env() | {"HOME": str(home)},
    )


def test_create_nodes_refuses_the_run_before_any_vm_when_a_world_is_still_on_scsi0(pve, tmp_path):
    """The order was a comment, and a per-node check would destroy 201 and only then find
    that 203's world never left its OS disk. So every node is decided first, read-only, and
    one unmigrated node stops the run with nothing stopped or destroyed."""
    result = create_nodes(pve, tmp_path)
    assert result.returncode != 0, result.stdout
    err = result.stderr
    assert "REFUSE" in err, err
    for vmid, node in NODES:
        assert f"{vmid} ({node})" in err, err
    # It names the order that has to be walked instead, and touches nothing.
    for step in ("15-ensure-data-disks.sh", "tasks/gameserver.py", "25-migrate-data.sh <node>"):
        assert step in err, err
    log = pve.call_log()
    assert "qm destroy" not in log and "qm stop" not in log and "qm create" not in log, log
    assert "heredoc" not in log, "a node was reached before every node had been decided"
    assert "scp " not in log, "an Ignition was pushed for a run that cannot be safe"


def test_create_nodes_destroys_once_every_node_is_on_its_host_zvol(pve, tmp_path):
    state = pve.read_state()
    for vmid, node in NODES:
        state["vms"][vmid]["scsi1"] = f"/dev/zvol/pool/mcdata/{node}"
    pve.write_state(state)

    result = create_nodes(pve, tmp_path)
    assert result.returncode == 0, result.stderr
    log = pve.call_log()
    # `$VMID` unexpanded: the destroy is in the script pve3 is handed, one node at a time.
    assert log.count("qm destroy $VMID --purge") == len(NODES), log


def test_create_nodes_owns_the_snippets_dir_and_attaches_data_disks():
    """Structural: the spike-era state above a rebuild has to be created by a tracked step."""
    script = (REPO / "scripts" / "fleet" / "20-create-nodes.sh").read_text()
    assert "mkdir -p /var/coreos/snippets" in script
    # The data disks are attached on every create, never in the offline render path.
    render_skip = script.index("if ((RENDER_ONLY)); then continue; fi")
    assert script.index("15-ensure-data-disks.sh", render_skip) > render_skip
    # The destroy stays as it is, and the header names the order that makes it safe: the
    # world has to be on the zvol (and off scsi0) before this script may destroy a VM.
    order = script[script.index("# ORDER MATTERS") : script.index("only irreversible one.")]
    cursor = 0
    for step in (
        "15-ensure-data-disks.sh",
        "tasks/gameserver.py",
        "25-migrate-data.sh <node>",
        "tasks/gameserver.py",
        "THIS SCRIPT",
    ):
        cursor = order.index(step, cursor)  # ValueError = the order is not written down

    assert "qm destroy \\$VMID --purge" in script
    assert "survives qm destroy --purge" in script
