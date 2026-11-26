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

REPO = Path(__file__).resolve().parent.parent
SSH_HOSTNAMES = {"plugins": "10.10.10.55", "lobby": "10.10.10.54"}


def deploy_ops(node: str) -> list[tuple[str, dict]]:
    ops: list[tuple[str, dict]] = []

    pyinfra = types.ModuleType("pyinfra")

    class Host:
        name = node
        data = types.SimpleNamespace(ssh_hostname=SSH_HOSTNAMES[node])

    pyinfra.host = Host()
    operations = types.ModuleType("pyinfra.operations")
    files = types.ModuleType("pyinfra.operations.files")
    server = types.ModuleType("pyinfra.operations.server")
    files.directory = lambda **kwargs: ops.append(("directory", kwargs))
    server.shell = lambda **kwargs: ops.append(("shell", kwargs))
    operations.files, operations.server = files, server
    injected = {
        "pyinfra": pyinfra,
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
