"""tasks/verify.py is what a human reads from the laptop to judge a node.

It already fails on a verdict that is not `ok` or on expected-jar drift. The one signal it
printed and shrugged off was the status ping: "no answer" was an echo, so a dead server
exited 0. That is exactly the state a digest that kills the JVM leaves behind.
"""
import os
import subprocess
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def verify_ops(node: str = "plugins") -> list[dict]:
    """The operations tasks/verify.py emits, by executing it against stub pyinfra modules."""
    ops: list[dict] = []
    pyinfra = types.ModuleType("pyinfra")

    class Host:
        name = node

    pyinfra.host = Host()
    operations = types.ModuleType("pyinfra.operations")
    server = types.ModuleType("pyinfra.operations.server")
    server.shell = lambda **kwargs: ops.append(kwargs)
    operations.server = server
    injected = {
        "pyinfra": pyinfra,
        "pyinfra.operations": operations,
        "pyinfra.operations.server": server,
    }
    saved = {name: sys.modules.get(name) for name in injected}
    sys.modules.update(injected)
    try:
        path = REPO / "tasks" / "verify.py"
        exec(compile(path.read_text(), str(path), "exec"), {"__file__": str(path)})
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    return ops


def ping_line(ops: list[dict], tmp_path: Path) -> subprocess.CompletedProcess:
    """Run the emitted mc-ping line with a stub `timeout`, which is what decides its status."""
    command = ops[0]["commands"]
    line = next(text for text in command.splitlines() if "mc-ping.py" in text)
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    (stub / "timeout").write_text(f"#!/bin/sh\nexit {os.environ['PING_EXIT']}\n")
    (stub / "timeout").chmod(0o755)
    return subprocess.run(
        ["bash", "-c", line],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"},
    )


def test_a_dead_server_fails_the_verify_run(tmp_path, monkeypatch):
    """`|| echo ...` printed the failure and carried on, so a node whose server is dead
    reported green. The run must fail and say which probe failed."""
    monkeypatch.setenv("PING_EXIT", "1")
    result = ping_line(verify_ops(), tmp_path)
    assert result.returncode != 0, result.stdout
    assert "mc-ping: no answer" in result.stdout


def test_an_answering_server_does_not_fail_the_run(tmp_path, monkeypatch):
    """The mirror: the guard is the ping's own status, not an unconditional exit."""
    monkeypatch.setenv("PING_EXIT", "0")
    result = ping_line(verify_ops(), tmp_path)
    assert result.returncode == 0, result.stderr
