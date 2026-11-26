"""scripts/fleet/reboot-gate.py decides whether the maintenance-window reboot may fire.

The gate is the only thing standing between an automatic OS update and a player's
world: a reboot while someone is connected rolls back the session, and the timer
fires on its own, unattended. So "there are players" must block. The deliberate
other half is that a server that is not answering does NOT block — an unreachable
server is not hosting players — which is a tradeoff, not an oversight (report §).
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "scripts" / "fleet" / "reboot-gate.py"

_spec = importlib.util.spec_from_file_location("reboot_gate", GATE)
reboot_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reboot_gate)


def test_online_players_block_the_reboot(capsys):
    assert reboot_gate.should_reboot({"players": {"online": 3, "max": 20}}) is False
    assert "skip: 3 players online" in capsys.readouterr().out


def test_empty_server_allows_the_reboot(capsys):
    assert reboot_gate.should_reboot({"players": {"online": 0, "max": 20}}) is True
    assert "reboot allowed" in capsys.readouterr().out


def test_unreachable_server_allows_the_reboot(capsys):
    assert reboot_gate.should_reboot(None) is True
    assert "not answering" in capsys.readouterr().out


def test_dry_run_prints_the_decision_and_does_not_reboot():
    """`--dry-run` is how the deploy proves the gate on a node without rebooting it."""
    out = subprocess.run(
        [sys.executable, str(GATE), "--dry-run"], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert "reboot allowed" in out.stdout or "skip:" in out.stdout
    assert "systemctl" not in out.stdout
