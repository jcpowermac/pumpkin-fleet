"""scripts/fleet/reboot-gate.py decides whether the maintenance-window reboot may fire.

The gate is the only thing standing between an automatic OS update and a player's
world: a reboot while someone is connected rolls back the session, and the timer
fires on its own, unattended. So "there are players" must block. The deliberate
other half is that a server that is not answering does NOT block — an unreachable
server is not hosting players — which is a tradeoff, not an oversight (report §).
"""
import importlib.util
import json
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "scripts" / "fleet" / "reboot-gate.py"
MC_PING = REPO / "scripts" / "mc-ping.py"

_spec = importlib.util.spec_from_file_location("reboot_gate", GATE)
reboot_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reboot_gate)


def varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def status_server(payload, raw=None):
    """A one-shot server speaking the status protocol, so mc-ping.py is exercised for real."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        conn, _ = listener.accept()
        with conn:
            conn.settimeout(5)
            seen = b""
            while not seen.endswith(b"\x01\x00"):  # handshake, then the status request
                chunk = conn.recv(4096)
                if not chunk:
                    return
                seen += chunk
            text = raw if raw is not None else json.dumps(payload).encode()
            conn.sendall(varint(len(varint(0) + varint(len(text)) + text)) + varint(0) + varint(len(text)) + text)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return port, listener


def ping_payload(payload) -> dict:
    port, listener = status_server(payload)
    try:
        out = subprocess.run(
            [sys.executable, "-I", str(MC_PING), "127.0.0.1", str(port)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        listener.close()
    assert out.returncode == 0, (out.returncode, out.stdout, out.stderr)
    return json.loads(out.stdout)


def test_online_players_block_the_reboot(capsys):
    assert reboot_gate.should_reboot({"players": {"online": 3, "max": 20}}) is False
    assert "skip: 3 players online" in capsys.readouterr().out


def test_empty_server_allows_the_reboot(capsys):
    assert reboot_gate.should_reboot({"players": {"online": 0, "max": 20}}) is True
    assert "reboot allowed" in capsys.readouterr().out


def test_unreachable_server_allows_the_reboot(capsys):
    assert reboot_gate.should_reboot(None) is True
    assert "not answering" in capsys.readouterr().out


def test_a_status_without_a_player_count_skips_the_reboot(capsys):
    """An unreachable server hosts nobody, but one that *answered* and hid its player
    count may well be hosting players: skip and say so. Raising here silently stopped
    every OS update reboot on the node, which is not the same thing as allowing it."""
    assert reboot_gate.should_reboot({"version": {"name": "Paper", "protocol": 777}}) is False
    assert reboot_gate.should_reboot({"players": None}) is False
    assert "no player count" in capsys.readouterr().out


def test_a_payload_with_players_but_no_description_or_version_still_blocks_the_reboot(capsys):
    """A status-rewriting plugin, a proxy or a version skew can answer with a partial
    payload. mc-ping.py used to KeyError on `description`/`version` before it ever looked at
    `players`, exit non-zero, and turn five players into "not answering" → reboot."""
    partial = {"players": {"online": 5, "max": 20}}
    printed = ping_payload(partial)
    assert printed["players"] == {"online": 5, "max": 20}
    assert printed["description"] is None and printed["version"] is None
    assert reboot_gate.should_reboot(printed) is False
    assert "skip: 5 players online" in capsys.readouterr().out


def test_a_payload_with_no_players_reports_null_and_skips(capsys):
    printed = ping_payload({"description": "a server", "version": {"name": "Paper"}})
    assert printed["players"] is None
    assert reboot_gate.should_reboot(printed) is False
    assert "no player count" in capsys.readouterr().out


def test_an_empty_server_still_allows_the_reboot_through_mc_ping(capsys):
    printed = ping_payload({"players": {"online": 0, "max": 20}, "version": {"name": "Paper"}})
    assert reboot_gate.should_reboot(printed) is True
    assert "reboot allowed" in capsys.readouterr().out


def ping_raw(payload, raw=None) -> subprocess.CompletedProcess:
    port, listener = status_server(payload, raw)
    try:
        return subprocess.run(
            [sys.executable, "-I", str(MC_PING), "127.0.0.1", str(port)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        listener.close()


def test_a_payload_that_is_not_a_json_object_is_unparseable_not_unreachable(capsys):
    """A status array (or any non-object JSON) made mc-ping raise AttributeError, exit 1, and
    read as "not answering" — which allows the reboot. It *did* answer, so it may well be the
    live server: the rule the two files share is that only silence allows a reboot."""
    out = ping_raw(["not", "an", "object"])
    assert out.returncode == reboot_gate.UNPARSEABLE, (out.returncode, out.stdout, out.stderr)
    assert "unparseable" in out.stderr.lower(), out.stderr
    # ...and what the gate does with that exit code: it answered, so do not reboot.
    assert reboot_gate.should_reboot({}) is False
    assert "no player count" in capsys.readouterr().out


def test_a_payload_that_is_not_valid_utf8_is_unparseable_not_unreachable():
    """json decodes the payload bytes, so an invalid-UTF-8 answer raised UnicodeDecodeError
    *before* json could call it malformed: exit 1, "not answering", reboot allowed. Same rule
    as the non-object payload above — an answer that cannot be read never allows a reboot."""
    out = ping_raw(None, raw=b'{"players": {"online": 4, "max": 20}}\xff')
    assert out.returncode == reboot_gate.UNPARSEABLE, (out.returncode, out.stdout, out.stderr)
    assert "unparseable" in out.stderr.lower(), out.stderr


def test_an_unparseable_answer_never_reaches_the_reboot(monkeypatch, tmp_path, capsys):
    """The gate's own path, not just should_reboot: mc-ping exiting 2 must not come back as
    the `None` that means "unreachable, reboot away"."""
    liar = tmp_path / "mc-ping.py"
    liar.write_text("import sys\nsys.exit(2)\n")
    monkeypatch.setattr(reboot_gate, "MC_PING", str(liar))
    assert reboot_gate.ping() == {}
    assert reboot_gate.should_reboot(reboot_gate.ping()) is False
    assert "no player count" in capsys.readouterr().out


def test_dry_run_prints_the_decision_and_does_not_reboot():
    """`--dry-run` is how the deploy proves the gate on a node without rebooting it."""
    out = subprocess.run(
        [sys.executable, str(GATE), "--dry-run"], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert "reboot allowed" in out.stdout or "skip:" in out.stdout
    assert "systemctl" not in out.stdout


@pytest.mark.parametrize("flag", ["--dryrun", "-n", "--dry-run=true"])
def test_an_unknown_flag_exits_two_and_never_reboots(flag):
    """A near-miss of --dry-run must not fall through to `systemctl reboot`."""
    out = subprocess.run(
        [sys.executable, str(GATE), flag], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 2, (out.returncode, out.stdout, out.stderr)
    assert "reboot" not in out.stdout
