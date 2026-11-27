#!/usr/bin/python3
"""Reboot into the staged OS deployment — unless somebody is playing.

Run by fleet-reboot.service, which fleet-reboot.timer fires inside the randomised
maintenance window. The OS update itself is automatic (AutomaticUpdatePolicy=stage),
so this is the half that decides *when* the new deployment becomes the running one.

A server that is not answering does NOT block the reboot: a dead container is not
hosting players, and blocking would wedge the node on a crashed server until a human
noticed. "Not answering" means silence only: an answer that carries no readable player
count — including one mc-ping could not parse at all — blocks, because that server may
well be hosting players (the two files share this one rule).

The cost of that choice: if the container is down because it crashed, the reboot takes
the crash evidence (the journal's podman logs survive, the container does not) with it,
and nothing here restarts or reports the server — the reboot is allowed, the container
comes back with the unit, and the crash is only visible as `podman ps -a` state plus
logs from before the reboot.

Usage: reboot-gate.py [--dry-run]   # --dry-run prints the decision, never reboots
"""
import argparse
import json
import subprocess
import sys

MC_PING = "/usr/local/lib/fleet/mc-ping.py"
UNPARSEABLE = 2   # mc-ping.py: the server answered, but the payload was not a JSON object


def ping() -> dict | None:
    """The status ping's JSON, {} when it answered unparseably, None when it never answered."""
    try:
        out = subprocess.run(
            [sys.executable, MC_PING, "127.0.0.1", "25565"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # An unparseable answer is still an answer: {} has no player count, which
        # should_reboot() reads as "may be hosting players, skip".
        if out.returncode == UNPARSEABLE:
            return {}
        return json.loads(out.stdout) if out.returncode == 0 else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def should_reboot(ping: dict | None) -> bool:
    if ping is None:
        print("reboot allowed: the server is not answering, so nobody is playing")
        return True
    players = ping.get("players")
    if not isinstance(players, dict) or "online" not in players:
        # It answered, so it may well be hosting players — unlike an unreachable server.
        # Skipping is the safe reading; raising here silently stopped every reboot.
        print("skip: the server answered but reported no player count")
        return False
    online = players["online"]
    if online:
        print(f"skip: {online} players online")
        return False
    print("reboot allowed: no players online")
    return True


def main() -> None:
    # argparse, not a substring scan: `--dryrun` / `-n` / `--dry-run=true` used to fall
    # through to the reboot. An unknown flag now exits 2 before anything is probed.
    parser = argparse.ArgumentParser(description="Reboot into the staged OS deployment.")
    parser.add_argument("--dry-run", action="store_true", help="print the decision, never reboot")
    args = parser.parse_args()
    if not should_reboot(ping()):
        return
    if args.dry_run:
        return
    subprocess.run(["systemctl", "reboot"], check=True)


if __name__ == "__main__":
    main()
